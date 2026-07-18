@dataclass
class sftp_SftpUser:
    index: int
    name: str
    password: str
    uid: str
    gid: str
    host_path: str = ''
    mode: str = 'rw'

@dataclass
class sftp_SftpBasic:
    container_name: str = ''
    hostname: str = ''
    image: str = ''
    restart: str = 'unless-stopped'
    cpuset: str = ''
    icon: str = ''
    data_path: str = ''
    data_mode: str = 'rw'
    host_port: str = '2222'
    container_port: str = '22'

@dataclass
class sftp_SftpNetwork:
    mode: str = 'lan_ip'
    network_name: str = ''
    ip_address: str = ''

class sftp_YamlCodec:

    def __init__(self) -> None:
        self.yaml = None
        self.pyyaml = None
        try:
            from ruamel.yaml import YAML
            y = YAML()
            y.preserve_quotes = True
            y.indent(mapping=4, sequence=4, offset=2)
            y.width = 4096
            self.yaml = y
        except Exception:
            try:
                import yaml
                self.pyyaml = yaml
            except Exception as exc:
                raise RuntimeError("Aucune librairie YAML disponible. Installe ruamel.yaml ou PyYAML dans l'image Flask System.") from exc

    def load(self, text: str) -> Any:
        if self.yaml is not None:
            return self.yaml.load(text) if text.strip() else {}
        return self.pyyaml.safe_load(text) if text.strip() else {}

    def dump(self, data: Any) -> str:
        if self.yaml is not None:
            buf = StringIO()
            self.yaml.dump(data, buf)
            return buf.getvalue()
        return self.pyyaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

def sftp_read_kv_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, 'r', encoding='utf-8') as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip().upper()
            if key:
                data[key] = value.strip().strip('"').strip("'")
    return data

def sftp_write_kv_file(path: str, data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    order = ['YAML', 'SERVICE', 'BROWSE_ROOTS', 'BACKUP_DIR', 'DOCKER_BIN', 'ENABLE_DOCKER_COMPOSE_CHECK']
    lines = ['# Configuration du module Flask SFTP', '# YAML = chemin complet vers le docker-compose/yml du Docker SFTP']
    for key in order:
        value = data.get(key, sftp_DEFAULT_CONFIG.get(key, ''))
        lines.append(f'{key}={value}')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip() + '\n')

def sftp_yaml_candidate_paths(folder: str) -> List[str]:
    """Retourne les .yml/.yaml du dossier Docker, sans imposer de nom précis."""
    folder = str(folder or '').strip()
    if not folder or not os.path.isdir(folder):
        return []
    paths: List[str] = []
    for root, dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        # Le dossier Docker YAML doit rester rapide à lire : racine + 1 niveau.
        if depth >= 1:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in {'backup', 'backups', '__pycache__'}]
        for name in files:
            low = name.lower()
            if low.endswith(('.yml', '.yaml')) and not low.startswith('.'):
                paths.append(os.path.join(root, name))
    return sorted(paths, key=lambda x: (os.path.basename(x).lower(), x.lower()))

def sftp_port_points_to_22(port: Any) -> bool:
    if isinstance(port, dict):
        return str(port.get('target') or port.get('container') or '').strip() == '22'
    text = str(port or '').strip().strip('\"\'')
    if not text:
        return False
    # Formats Compose courants : 2222:22, 192.168.1.10:2222:22, 22/tcp.
    target = text.split('/')[0].split(':')[-1].strip()
    return target == '22'

def sftp_score_yaml_service(service_name: str, service: Any) -> int:
    if not isinstance(service, dict):
        return 0
    score = 0
    name_low = str(service_name or '').lower()
    hay_parts = [name_low]
    for key in ('container_name', 'hostname', 'image'):
        hay_parts.append(str(service.get(key) or '').lower())
    hay = ' '.join(hay_parts)
    if 'sftp' in name_low:
        score += 100
    if 'sftp' in hay:
        score += 70
    if 'openssh' in hay:
        score += 45
    if re.search(r'(^|[^a-z])ssh([^a-z]|$)', hay):
        score += 25
    env_keys = []
    for item in sftp_env_to_list(service.get('environment')):
        key, _value = sftp_split_env_item(item)
        env_keys.append(key)
    if any(re.fullmatch(r'USERS_VAR\d+', key or '') for key in env_keys):
        score += 80
    if any(str(key).startswith('SSH_') for key in env_keys):
        score += 35
    if 'KEY_VAR' in env_keys:
        score += 20
    ports = service.get('ports') if isinstance(service.get('ports'), list) else []
    if any(sftp_port_points_to_22(port) for port in ports):
        score += 45
    volumes = service.get('volumes') if isinstance(service.get('volumes'), list) else []
    for raw in volumes:
        _source, target, _mode = sftp_parse_volume(raw)
        if target == '/data':
            score += 15
        if re.fullmatch(r'/home/[^/]+/Data', target or ''):
            score += 30
            break
    return score


def sftp_best_yaml_service(data: Any) -> Tuple[str, int]:
    services = data.get('services') if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return '', 0
    best_name = ''
    best_score = 0
    for service_name, service in services.items():
        score = sftp_score_yaml_service(str(service_name), service)
        if score > best_score:
            best_name = str(service_name)
            best_score = score
    return best_name, best_score


def sftp_detect_yaml_from_docker_folder() -> Tuple[str, str, str]:
    """Détecte le YAML SFTP depuis le dossier YAML déclaré dans Docker.

    Retourne (yaml_path, service_name, message). Aucun nom de fichier n'est imposé :
    on inspecte le contenu des YAML et on cherche un vrai service SFTP/SSH.
    """
    yml_folder, setup_error = svc_docker_yml_folder_status()
    if setup_error:
        return '', '', setup_error
    best: Tuple[int, str, str] = (0, '', '')
    codec = sftp_YamlCodec()
    for path in sftp_yaml_candidate_paths(yml_folder):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                data = codec.load(handle.read()) or {}
        except Exception:
            continue
        services = data.get('services') if isinstance(data, dict) else None
        if not isinstance(services, dict):
            continue
        service_name, score = sftp_best_yaml_service(data)
        if 'sftp' in os.path.basename(path).lower():
            score += 20
        if score > best[0]:
            best = (score, path, service_name)
    if best[0] >= 60 and best[1]:
        return best[1], best[2], f'YAML SFTP détecté automatiquement : {best[1]} / service {best[2]}'
    return '', '', (
        f'Dossier YAML Docker : {yml_folder}\n'
        'Aucun YAML SFTP détecté automatiquement. Vérifie qu’un fichier .yml/.yaml contient un service SFTP/SSH '
        'avec un container/image SFTP, le port 22, USERS_VAR ou des variables SSH_.'
    )

def sftp_complete_config_from_autodetect(conf: Dict[str, str]) -> Tuple[Dict[str, str], bool, str]:
    changed = False
    message = ''
    conf = conf.copy()
    yaml_path = str(conf.get('YAML') or '').strip()
    yaml_ok = bool(yaml_path and os.path.exists(yaml_path))
    if yaml_ok:
        try:
            data, _codec = sftp_load_yaml_file(yaml_path, conf)
            service_name = sftp_choose_service(data, conf.get('SERVICE', ''))
            current_score = sftp_score_yaml_service(service_name, data.get('services', {}).get(service_name, {}))
            if current_score < 60:
                detected_yaml, detected_service, message = sftp_detect_yaml_from_docker_folder()
                if detected_yaml and os.path.realpath(detected_yaml) != os.path.realpath(yaml_path):
                    conf['YAML'] = detected_yaml
                    conf['SERVICE'] = detected_service
                    changed = True
            elif service_name and service_name != conf.get('SERVICE', ''):
                conf['SERVICE'] = service_name
                changed = True
        except Exception:
            pass
    else:
        detected_yaml, detected_service, message = sftp_detect_yaml_from_docker_folder()
        if detected_yaml:
            if conf.get('YAML') != detected_yaml:
                conf['YAML'] = detected_yaml
                changed = True
            if detected_service and conf.get('SERVICE') != detected_service:
                conf['SERVICE'] = detected_service
                changed = True
    # Valeurs internes historiques conservées, mais plus exposées dans l'interface.
    defaults = {
        'BROWSE_ROOTS': '/',
        'BACKUP_DIR': '../backups/sftp',
        'DOCKER_BIN': 'docker',
        'ENABLE_DOCKER_COMPOSE_CHECK': conf.get('ENABLE_DOCKER_COMPOSE_CHECK') or sftp_DEFAULT_CONFIG.get('ENABLE_DOCKER_COMPOSE_CHECK', '1'),
    }
    for key, value in defaults.items():
        if conf.get(key, '') != value:
            conf[key] = value
            changed = True
    return conf, changed, message

def sftp_get_config() -> Dict[str, str]:
    conf = sftp_DEFAULT_CONFIG.copy()
    conf.update(sftp_read_kv_file(sftp_CONFIG_FILE))
    conf, changed, _message = sftp_complete_config_from_autodetect(conf)
    if changed or not os.path.exists(sftp_CONFIG_FILE):
        sftp_write_kv_file(sftp_CONFIG_FILE, conf)
    return conf
sftp_ENV_VAR_RE = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\\}')

def sftp_strip_env_quotes(value: str) -> str:
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and (value[0] in {"'", '"'}):
        return value[1:-1]
    return value

def sftp_env_file_path(conf: Dict[str, str]) -> str:
    """Le .env vient du même dossier YAML que le module Docker."""
    return svc_env_file_from_docker_yaml(conf)

def sftp_read_env_file(path: str) -> Dict[str, str]:
    """
    Lecture .env Docker Compose case-sensitive.
    Important : ${ip_sftpfolders}, ${docker_type} et ${REGISTRY}
    gardent exactement leur casse.
    """
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key:
                data[key] = sftp_strip_env_quotes(value)
    return data

def sftp_get_env_map(conf: Dict[str, str]) -> Dict[str, str]:
    return sftp_read_env_file(sftp_env_file_path(conf))

def sftp_resolve_env_string(value: str, env_map: Dict[str, str]) -> str:
    text = str(value or '')

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(3)
        if key in env_map:
            return env_map[key]
        return default or ''
    return sftp_ENV_VAR_RE.sub(repl, text)

def sftp_resolve_env_tree(value: Any, env_map: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return sftp_resolve_env_string(value, env_map)
    if isinstance(value, list):
        return [sftp_resolve_env_tree(item, env_map) for item in value]
    if isinstance(value, dict):
        return {key: sftp_resolve_env_tree(item, env_map) for key, item in value.items()}
    return value

def sftp_env_keys_in(value: Any) -> List[str]:
    return [m.group(1) for m in sftp_ENV_VAR_RE.finditer(str(value or ''))]

def sftp_env_prefix_candidates(env_map: Dict[str, str]) -> List[Tuple[str, str]]:
    preferred = []
    normal = []
    for key, value in env_map.items():
        if not value:
            continue
        item = (key, value)
        if key.startswith('PATH_') or key in {'REGISTRY', 'BR0_NAME'} or key.startswith('ip_'):
            preferred.append(item)
        else:
            normal.append(item)
    return sorted(preferred, key=lambda x: len(x[1]), reverse=True) + sorted(normal, key=lambda x: len(x[1]), reverse=True)

def sftp_replace_prefix_with_env(value: str, key: str, env_value: str) -> str:
    if not env_value:
        return value
    if value == env_value:
        return '${' + key + '}'
    if value.startswith(env_value.rstrip('/') + '/'):
        return '${' + key + '}' + value[len(env_value.rstrip('/')):]
    if value.startswith(env_value + '/'):
        return '${' + key + '}' + value[len(env_value):]
    return value

def sftp_templatize_value(value: str, old_raw: str='', env_map: Optional[Dict[str, str]]=None) -> str:
    """
    Convertit une valeur affichée dans le formulaire vers une valeur YAML.
    Si le YAML contenait ${VAR}, on garde cette variable quand elle représente
    la même valeur une fois résolue.

    Les chemins utilisateurs SFTP ne doivent pas passer ici : on les garde
    volontairement en dur pour que le bouton Parcourir reste simple.
    """
    env_map = env_map or {}
    value = str(value or '').strip()
    old_raw = str(old_raw or '').strip()
    if old_raw and sftp_resolve_env_string(old_raw, env_map) == value:
        return old_raw
    for key in sftp_env_keys_in(old_raw):
        if key in env_map:
            replaced = sftp_replace_prefix_with_env(value, key, env_map[key])
            if replaced != value:
                value = replaced
    for key, env_value in sftp_env_prefix_candidates(env_map):
        replaced = sftp_replace_prefix_with_env(value, key, env_value)
        if replaced != value:
            value = replaced
            break
    return value

def sftp_env_setting_value(key: str, value: str, old_raw: str, env_map: Dict[str, str]) -> str:
    value = str(value or '').strip()
    old_raw = str(old_raw or '').strip()
    if old_raw and sftp_resolve_env_string(old_raw, env_map) == value:
        return old_raw
    if key in env_map and env_map[key] == value:
        return '${' + key + '}'
    return sftp_templatize_value(value, old_raw, env_map)

def sftp_normalize_path(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    return os.path.normpath(value.replace('\\', '/'))

def sftp_bool_conf(conf: Dict[str, str], key: str, default: str='0') -> bool:
    return str(conf.get(key, default)).strip().lower() in {'1', 'true', 'yes', 'on'}

def sftp_allowed_roots(conf: Dict[str, str]) -> List[str]:
    roots = []
    for raw in conf.get('BROWSE_ROOTS', '/mnt/user').split(','):
        raw = sftp_normalize_path(raw)
        if raw:
            roots.append(os.path.realpath(raw))
    return roots or ['/mnt/user']

def sftp_is_under_allowed_root(path: str, roots: List[str]) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    for root in roots:
        if root == '/':
            return True
        if real == root or real.startswith(root.rstrip('/') + '/'):
            return True
    return False

def sftp_yaml_missing_message(conf: Optional[Dict[str, str]] = None, path: str = '') -> str:
    yml_folder, setup_error = svc_docker_yml_folder_status()
    lines: List[str] = []
    if setup_error:
        lines.append('❌ Module Docker : dossier YAML non configuré')
        lines.append(setup_error)
        return '\n'.join(lines)
    if yml_folder and os.path.isdir(yml_folder):
        lines.append(f'✅ Dossier YAML Docker : {yml_folder}')
    else:
        lines.append(f'❌ Dossier YAML Docker introuvable : {yml_folder or "non configuré"}')
    _detected_yaml, _detected_service, detect_message = sftp_detect_yaml_from_docker_folder()
    if path:
        lines.append(f'❌ YAML configuré introuvable : {path}')
    lines.append('❌ Aucun YAML SFTP utilisable détecté automatiquement.')
    if detect_message:
        lines.append(detect_message)
    return '\n'.join(lines)

def sftp_load_yaml_file(path: str, conf: Optional[Dict[str, str]] = None) -> Tuple[Any, sftp_YamlCodec]:
    codec = sftp_YamlCodec()
    if not path or not os.path.exists(path):
        raise FileNotFoundError(sftp_yaml_missing_message(conf, path))
    with open(path, 'r', encoding='utf-8') as handle:
        return (codec.load(handle.read()) or {}, codec)

def sftp_choose_service(data: Any, configured_service: str='') -> str:
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict) or (not data['services']):
        raise ValueError('Le YAML ne contient pas de bloc services valide.')
    services = data['services']
    configured_service = (configured_service or '').strip()
    if configured_service and configured_service in services:
        configured_score = sftp_score_yaml_service(configured_service, services.get(configured_service))
        if configured_score >= 60:
            return configured_service
        best_name, best_score = sftp_best_yaml_service(data)
        if best_name and best_score >= 60:
            return best_name
        return configured_service
    best_name, best_score = sftp_best_yaml_service(data)
    if best_name and best_score >= 60:
        return best_name
    for name in services.keys():
        lowered = str(name).lower()
        if 'sftp' in lowered or 'ssh' in lowered:
            return str(name)
    return str(next(iter(services.keys())))

def sftp_env_to_list(env: Any) -> List[str]:
    if env is None:
        return []
    if isinstance(env, list):
        return [str(item) for item in env]
    if isinstance(env, dict):
        return [f'{key}={value}' for key, value in env.items()]
    return []

def sftp_split_env_item(item: str) -> Tuple[str, str]:
    if '=' not in item:
        return (item.strip(), '')
    key, value = item.split('=', 1)
    return (key.strip(), value.strip())

def sftp_env_dict(env_items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = sftp_DEFAULT_ENV.copy()
    for item in env_items:
        key, value = sftp_split_env_item(item)
        if key and (not re.fullmatch('USERS_VAR\\d+', key)):
            out[key] = value
    return out

def sftp_parse_users(env_items: List[str], volumes: List[Any]) -> List[sftp_SftpUser]:
    volume_by_user: Dict[str, Tuple[str, str]] = {}
    for raw in volumes:
        source, target, mode = sftp_parse_volume(raw)
        m = re.fullmatch('/home/([^/]+)/Data', target or '')
        if m:
            volume_by_user[m.group(1)] = (source, mode if mode in {'ro', 'rw'} else 'rw')
    out: List[sftp_SftpUser] = []
    for item in env_items:
        key, value = sftp_split_env_item(item)
        m = re.fullmatch('USERS_VAR(\\d+)', key)
        if not m:
            continue
        parts = value.split(':')
        while len(parts) < 4:
            parts.append('')
        user = sftp_SftpUser(int(m.group(1)), parts[0], parts[1], parts[2], parts[3])
        hp, mode = volume_by_user.get(user.name, ('', 'rw'))
        user.host_path = hp
        user.mode = mode
        out.append(user)
    return sorted(out, key=lambda x: x.index)

def sftp_parse_volume(raw: Any) -> Tuple[str, str, str]:
    if isinstance(raw, dict):
        source = str(raw.get('source') or raw.get('src') or '').strip()
        target = str(raw.get('target') or raw.get('dst') or raw.get('destination') or '').strip()
        read_only = raw.get('read_only')
        mode = 'ro' if read_only is True else 'rw'
        return (source, target, mode)
    text = str(raw or '').strip()
    if not text:
        return ('', '', '')
    parts = text.split(':')
    if len(parts) >= 3:
        return (parts[0].strip(), parts[1].strip(), parts[2].strip() or 'rw')
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip(), 'rw')
    return ('', '', '')

def sftp_compose_volume(source: str, target: str, mode: str) -> str:
    mode = 'ro' if mode == 'ro' else 'rw'
    return f'{source}:{target}:{mode}'

def sftp_parse_ports(ports: Any) -> Tuple[str, str]:
    if not isinstance(ports, list) or not ports:
        return ('2222', '22')
    raw = ports[0]
    if isinstance(raw, int):
        if raw > 65535:
            return (str(raw // 60), str(raw % 60))
        return ('2222', str(raw or 22))
    if isinstance(raw, dict):
        published = str(raw.get('published') or raw.get('host_port') or '2222')
        target = str(raw.get('target') or raw.get('container_port') or '22')
        return (published, target)
    text = str(raw).strip().strip('"').strip("'")
    if not text:
        return ('2222', '22')
    parts = text.split(':')
    if len(parts) >= 2:
        return (parts[-2].strip(), parts[-1].split('/')[0].strip())
    return ('2222', parts[0].split('/')[0].strip() or '22')

def sftp_compose_port(host_port: str, container_port: str) -> str:
    host_port = str(host_port or '2222').strip()
    container_port = str(container_port or '22').strip()
    return f'{host_port}:{container_port}'

def sftp_get_label(service: Dict[str, Any], key: str) -> str:
    labels = service.get('labels')
    if isinstance(labels, dict):
        return str(labels.get(key, '') or '')
    if isinstance(labels, list):
        for item in labels:
            k, v = sftp_split_env_item(str(item))
            if k == key:
                return v
    return ''

def sftp_set_label(service: Dict[str, Any], key: str, value: str) -> None:
    labels = service.get('labels')
    if not value:
        if isinstance(labels, dict):
            labels.pop(key, None)
        elif isinstance(labels, list):
            service['labels'] = [x for x in labels if sftp_split_env_item(str(x))[0] != key]
        return
    if isinstance(labels, dict):
        labels[key] = value
    elif isinstance(labels, list):
        out = []
        done = False
        for item in labels:
            k, _v = sftp_split_env_item(str(item))
            if k == key:
                out.append(f'{key}={value}')
                done = True
            else:
                out.append(item)
        if not done:
            out.append(f'{key}={value}')
        service['labels'] = out
    else:
        service['labels'] = {key: value}

def sftp_parse_basic(service: Dict[str, Any], volumes: List[Any]) -> sftp_SftpBasic:
    data_path = ''
    data_mode = 'rw'
    for raw in volumes:
        source, target, mode = sftp_parse_volume(raw)
        if target == '/data':
            data_path = source
            data_mode = mode if mode in {'ro', 'rw'} else 'rw'
            break
    host_port, container_port = sftp_parse_ports(service.get('ports'))
    return sftp_SftpBasic(container_name=str(service.get('container_name', '') or ''), hostname=str(service.get('hostname', '') or ''), image=str(service.get('image', '') or ''), restart=str(service.get('restart', 'unless-stopped') or 'unless-stopped'), cpuset=str(service.get('cpuset', '') or ''), icon=sftp_get_label(service, 'net.unraid.docker.icon'), data_path=data_path, data_mode=data_mode, host_port=host_port, container_port=container_port)

def sftp_parse_network(service: Dict[str, Any]) -> sftp_SftpNetwork:
    mode = str(service.get('network_mode', '') or '').strip().lower()
    if mode == 'bridge':
        return sftp_SftpNetwork('bridge', '', '')
    if mode == 'host':
        return sftp_SftpNetwork('host', '', '')
    networks = service.get('networks')
    if isinstance(networks, dict) and networks:
        name = str(next(iter(networks.keys())))
        cfg = networks.get(name) or {}
        ip = ''
        if isinstance(cfg, dict):
            ip = str(cfg.get('ipv4_address') or cfg.get('ip_address') or '')
        return sftp_SftpNetwork('lan_ip' if ip else 'lan', name, ip)
    if isinstance(networks, list) and networks:
        return sftp_SftpNetwork('lan', str(networks[0]), '')
    return sftp_SftpNetwork('bridge', '', '')

def sftp_docker_cmd(conf: Dict[str, str], args: List[str], timeout: int=8) -> Tuple[int, str]:
    cmd = [conf.get('DOCKER_BIN', 'docker') or 'docker'] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.returncode, ((p.stdout or '') + (p.stderr or '')).strip())
    except Exception as exc:
        return (1, str(exc))

def sftp_is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(str(value or '').strip())
        return True
    except Exception:
        return False

def sftp_host_primary_ip() -> str:
    """Adresse IPv4 principale de l'hôte, utile pour les containers en network_mode: host."""
    try:
        p = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True, text=True, timeout=3)
        if p.returncode == 0:
            parts = (p.stdout or '').strip().split()
            for i, part in enumerate(parts):
                if part == 'src' and i + 1 < len(parts) and sftp_is_valid_ip(parts[i + 1]):
                    return parts[i + 1]
    except Exception:
        pass
    try:
        p = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=3)
        if p.returncode == 0:
            for part in (p.stdout or '').strip().split():
                if sftp_is_valid_ip(part):
                    return part
    except Exception:
        pass
    return ''

def sftp_container_ip_from_inspect(container_data: Dict[str, Any], preferred_network: str = '', fallback_ip: str = '') -> str:
    """Retourne l'IP réellement vue par Docker, ou l'IP hôte en mode host.

    Docker ne range pas toujours l'adresse au même endroit selon le driver
    réseau et la version : bridge/macvlan/ipvlan mettent souvent
    `IPAddress`, mais une IP fixe peut aussi se retrouver dans
    `IPAMConfig.IPv4Address`. On lit donc les deux formes avant de tomber
    sur les valeurs YAML ou l'IP de l'hôte.
    """
    host_config = container_data.get('HostConfig') if isinstance(container_data, dict) else {}
    network_mode = str((host_config or {}).get('NetworkMode') or '').strip().lower()
    if network_mode == 'host':
        return sftp_host_primary_ip() or 'host'

    network_settings = container_data.get('NetworkSettings') if isinstance(container_data, dict) else {}
    networks = (network_settings or {}).get('Networks') or {}
    candidates: List[str] = []

    def add_network_candidates(cfg: Any) -> None:
        if not isinstance(cfg, dict):
            return
        candidates.append(str(cfg.get('IPAddress') or '').strip())
        candidates.append(str(cfg.get('GlobalIPv6Address') or '').strip())
        ipam_cfg = cfg.get('IPAMConfig') or {}
        if isinstance(ipam_cfg, dict):
            candidates.append(str(ipam_cfg.get('IPv4Address') or '').strip())
            candidates.append(str(ipam_cfg.get('IPv6Address') or '').strip())

    if preferred_network and isinstance(networks, dict) and preferred_network in networks:
        add_network_candidates(networks.get(preferred_network) or {})
    if isinstance(networks, dict):
        for cfg in networks.values():
            add_network_candidates(cfg)

    candidates.append(str((network_settings or {}).get('IPAddress') or '').strip())
    candidates.append(str(fallback_ip or '').strip())
    for ip in candidates:
        if ip and sftp_is_valid_ip(ip):
            return ip
    return str(fallback_ip or '').strip()


def sftp_container_ip_from_docker_cli(conf: Dict[str, str], container_name: str) -> str:
    """Fallback direct Docker CLI pour l'IP affichée sur la page main.

    Cette méthode reprend l'esprit de la page Docker : si Docker sait afficher
    une IP dans `NetworkSettings.Networks`, on l'utilise. En mode host, on
    affiche l'IP principale de l'hôte.
    """
    container_name = str(container_name or '').strip()
    if not container_name:
        return ''
    templates = [
        '{{.HostConfig.NetworkMode}}',
        '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}',
        '{{range .NetworkSettings.Networks}}{{if .GlobalIPv6Address}}{{.GlobalIPv6Address}} {{end}}{{end}}',
        '{{range .NetworkSettings.Networks}}{{with .IPAMConfig}}{{if .IPv4Address}}{{.IPv4Address}} {{end}}{{end}}{{end}}',
        '{{.NetworkSettings.IPAddress}}',
    ]
    rc, out = sftp_docker_cmd(conf, ['inspect', '--format', templates[0], container_name], timeout=8)
    if rc == 0 and str(out or '').strip().lower() == 'host':
        return sftp_host_primary_ip() or 'host'
    for tmpl in templates[1:]:
        rc, out = sftp_docker_cmd(conf, ['inspect', '--format', tmpl, container_name], timeout=8)
        if rc != 0:
            continue
        for token in re.split(r'[\s,;]+', str(out or '').strip()):
            token = token.strip().strip('[]')
            if token and sftp_is_valid_ip(token):
                return token
    return ''

def sftp_detect_docker_networks(conf: Dict[str, str]) -> List[Dict[str, str]]:
    rc, out = sftp_docker_cmd(conf, ['network', 'ls', '--format', '{{.Name}}'], timeout=8)
    if rc != 0:
        return []
    result: List[Dict[str, str]] = []
    for name in [x.strip() for x in out.splitlines() if x.strip()]:
        if name in {'host', 'none', 'bridge'}:
            continue
        rc2, raw = sftp_docker_cmd(conf, ['network', 'inspect', name], timeout=8)
        driver = ''
        subnet = ''
        gateway = ''
        if rc2 == 0:
            try:
                data = json.loads(raw)[0]
                driver = str(data.get('Driver') or '')
                ipam = data.get('IPAM') or {}
                cfgs = ipam.get('Config') or []
                if cfgs:
                    subnet = str(cfgs[0].get('Subnet') or '')
                    gateway = str(cfgs[0].get('Gateway') or '')
            except Exception:
                pass
        result.append({'name': name, 'driver': driver, 'subnet': subnet, 'gateway': gateway})
    result.sort(key=lambda x: (0 if x.get('name', '').lower().startswith('br') else 1, x.get('name', '').lower()))
    return result

def sftp_current_state(conf: Dict[str, str]) -> Dict[str, Any]:
    if conf.get('_DOCKER_YAML_ERROR'):
        raise FileNotFoundError(conf['_DOCKER_YAML_ERROR'])
    env_map = sftp_get_env_map(conf)
    data, _codec = sftp_load_yaml_file(conf['YAML'], conf)
    resolved_data = sftp_resolve_env_tree(copy.deepcopy(data), env_map)
    service_name = sftp_choose_service(data, conf.get('SERVICE', ''))
    service = data['services'][service_name]
    resolved_service = resolved_data['services'][service_name]
    env_items = sftp_env_to_list(resolved_service.get('environment'))
    raw_env_items = sftp_env_to_list(service.get('environment'))
    volumes = resolved_service.get('volumes') if isinstance(resolved_service.get('volumes'), list) else []
    raw_volumes = service.get('volumes') if isinstance(service.get('volumes'), list) else []
    basic = sftp_parse_basic(resolved_service, volumes)
    network = sftp_parse_network(resolved_service)
    return {'data': data, 'resolved_data': resolved_data, 'service_name': service_name, 'service': resolved_service, 'raw_service': service, 'basic': basic, 'network': network, 'env': sftp_env_dict(env_items), 'users': sftp_parse_users(env_items, volumes), 'env_items': env_items, 'raw_env_items': raw_env_items, 'volumes': volumes, 'raw_volumes': raw_volumes, 'env_file': sftp_env_file_path(conf), 'env_map': env_map}

def sftp_validate_users(users: List[sftp_SftpUser], roots: List[str]) -> List[str]:
    errors: List[str] = []
    seen = set()
    for user in users:
        if not user.name:
            errors.append('Un utilisateur a un nom vide.')
            continue
        if not sftp_USER_RE.fullmatch(user.name):
            errors.append(f'Utilisateur invalide : {user.name}. Utilise minuscules, chiffres, _ et -.')
        if user.name in seen:
            errors.append(f'Utilisateur en double : {user.name}')
        seen.add(user.name)
        if not user.password:
            errors.append(f'Mot de passe manquant pour {user.name}. Même avec clé SSH, le format USERS_VAR demande un champ pass.')
        if ':' in user.password:
            errors.append(f"Mot de passe refusé pour {user.name} : le caractère ':' est interdit avec le format USERS_VAR.")
        if not user.uid.isdigit():
            errors.append(f'UID invalide pour {user.name} : {user.uid}')
        if not user.gid.isdigit():
            errors.append(f'GID invalide pour {user.name} : {user.gid}')
        if not user.host_path.startswith('/'):
            errors.append(f'Chemin hôte invalide pour {user.name} : {user.host_path}')
        elif not sftp_is_under_allowed_root(user.host_path, roots):
            errors.append(f'Chemin hôte refusé pour {user.name} : hors BROWSE_ROOTS.')
        if user.mode not in {'rw', 'ro'}:
            errors.append(f'Mode invalide pour {user.name} : {user.mode}')
    return errors

def sftp_validate_basic(basic: sftp_SftpBasic, roots: List[str]) -> List[str]:
    errors: List[str] = []
    if basic.restart not in {'no', 'always', 'unless-stopped', 'on-failure'}:
        errors.append('Politique de redémarrage invalide.')
    if basic.data_path:
        if not basic.data_path.startswith('/'):
            errors.append(f'Chemin /data invalide : {basic.data_path}')
        elif not sftp_is_under_allowed_root(basic.data_path, roots):
            errors.append(f'Chemin /data refusé : hors BROWSE_ROOTS.')
    else:
        errors.append('Volume /data manquant : il contient config, clés serveur, clés privées et users.conf.')
    for label, value in (('Port hôte', basic.host_port), ('Port container', basic.container_port)):
        if not str(value).isdigit() or not 1 <= int(value) <= 65535:
            errors.append(f'{label} invalide : {value}')
    return errors

def sftp_validate_network(network: sftp_SftpNetwork, detected: List[Dict[str, str]]) -> List[str]:
    errors: List[str] = []
    if network.mode not in {'bridge', 'lan', 'lan_ip'}:
        errors.append('Mode réseau invalide. Utilise bridge, LAN ou LAN IP fixe.')
    if network.mode in {'lan', 'lan_ip'} and (not network.network_name):
        errors.append('Réseau LAN manquant.')
    if network.mode == 'lan_ip':
        if not network.ip_address:
            errors.append('Adresse IP fixe manquante pour le mode LAN IP fixe.')
        else:
            try:
                ipaddress.ip_address(network.ip_address)
            except ValueError:
                errors.append(f'Adresse IP fixe invalide : {network.ip_address}')
    return errors

def sftp_normalize_yes_no(value: str, default: str='no') -> str:
    v = (value or default).strip().lower()
    return 'yes' if v in {'1', 'true', 'yes', 'on', 'oui'} else 'no'

def sftp_list_linux_users(existing_users: Optional[List[sftp_SftpUser]] = None) -> List[Dict[str, Any]]:
    """Utilisateurs Linux proposés pour le Docker SFTP.

    L'interface ne demande plus UID/GID à la main : elle les reprend de /etc/passwd.
    Les comptes système/nologin sont masqués, sauf s'ils existent déjà dans le YAML
    afin de ne pas faire disparaître une ancienne configuration au premier affichage.
    """
    existing_by_name = {u.name: u for u in (existing_users or []) if getattr(u, 'name', '')}
    existing_names = set(existing_by_name.keys())
    blocked_shells = {'/usr/sbin/nologin', '/sbin/nologin', '/bin/false', 'nologin', 'false'}
    users: List[Dict[str, Any]] = []
    seen = set()
    try:
        entries = pwd.getpwall()
    except Exception:
        entries = []
    for entry in entries:
        name = str(getattr(entry, 'pw_name', '') or '').strip()
        if not name or not sftp_USER_RE.fullmatch(name):
            continue
        uid = int(getattr(entry, 'pw_uid', 0) or 0)
        gid = int(getattr(entry, 'pw_gid', 0) or 0)
        shell = str(getattr(entry, 'pw_shell', '') or '').strip().lower()
        is_existing = name in existing_names
        is_normal_user = uid >= 1000 and shell not in blocked_shells
        if not (is_normal_user or is_existing):
            continue
        group_name = ''
        try:
            group_name = grp.getgrgid(gid).gr_name
        except Exception:
            group_name = str(gid)
        users.append({
            'name': name,
            'uid': str(uid),
            'gid': str(gid),
            'group': group_name,
            'label': f'{name} ({uid}:{gid})',
            'missing': False,
        })
        seen.add(name)
    # Sécurité anti-perte : si le YAML contient déjà un compte absent de /etc/passwd,
    # on l'affiche quand même comme ancien compte, mais il reste visible comme absent Linux.
    for name, user in existing_by_name.items():
        if name in seen:
            continue
        users.append({
            'name': name,
            'uid': str(user.uid or ''),
            'gid': str(user.gid or ''),
            'group': '',
            'label': f'{name} ({user.uid}:{user.gid}) - absent Linux',
            'missing': True,
        })
    return sorted(users, key=lambda item: (bool(item.get('missing')), int(item.get('uid') or 999999), str(item.get('name') or '').lower()))


def sftp_linux_user_lookup() -> Dict[str, Dict[str, Any]]:
    return {item['name']: item for item in sftp_list_linux_users([])}


def sftp_collect_env_from_form(existing: Dict[str, str]) -> Dict[str, str]:
    """
    Formulaire métier SFTP : on ne modifie plus TZ/PUID/PGID ni les paramètres Docker.
    Ces valeurs restent dans le YAML/.env. Ici on ne pilote que les options SSH propres au service.
    """
    out = existing.copy()
    key_var = request.form.get('env_KEY_VAR', out.get('KEY_VAR', '3072')).strip() or '3072'
    if key_var.lower() == 'ed25519':
        out['KEY_VAR'] = 'ed25519'
    elif key_var in {'2048', '3072', '4096'}:
        out['KEY_VAR'] = key_var
    else:
        out['KEY_VAR'] = '3072'
    for key in sftp_YES_NO_KEYS:
        posted = request.form.get(f'env_{key}')
        if posted is None and request.form.get(f'env_{key}__off') is not None:
            posted = 'no'
        if posted is None:
            posted = out.get(key, sftp_DEFAULT_ENV.get(key, 'no'))
        out[key] = sftp_normalize_yes_no(posted, sftp_DEFAULT_ENV.get(key, 'no'))
    return out

def sftp_collect_users_from_form(existing_users: List[sftp_SftpUser]) -> List[sftp_SftpUser]:
    names = request.form.getlist('user_name[]')
    original = request.form.getlist('user_original[]')
    passwords = request.form.getlist('user_password[]')
    uids = request.form.getlist('user_uid[]')
    gids = request.form.getlist('user_gid[]')
    host_paths = request.form.getlist('user_host_path[]')
    modes = request.form.getlist('user_mode[]')
    linux_users = sftp_linux_user_lookup()
    existing_by_name = {u.name: u for u in existing_users}
    users: List[sftp_SftpUser] = []
    count = max(len(names), len(passwords), len(uids), len(gids), len(host_paths), len(modes), len(original))
    for i in range(count):
        name = names[i].strip() if i < len(names) else ''
        old_name = original[i].strip() if i < len(original) else name
        host_path = sftp_normalize_path(host_paths[i]) if i < len(host_paths) else ''
        if not any([name, host_path]):
            continue
        # Le mot de passe Linux n'est jamais lu ni stocké en clair.
        # Le Docker SFTP garde un champ password technique dans USERS_VAR : on écrit 0000.
        password = '0000'
        if name in linux_users:
            uid = str(linux_users[name].get('uid') or '')
            gid = str(linux_users[name].get('gid') or '')
        elif name in existing_by_name:
            # Ancienne ligne déjà présente dans le YAML mais absente du Linux courant.
            uid = str(existing_by_name[name].uid or '')
            gid = str(existing_by_name[name].gid or '')
        elif old_name in existing_by_name:
            uid = str(existing_by_name[old_name].uid or '')
            gid = str(existing_by_name[old_name].gid or '')
        else:
            uid = uids[i].strip() if i < len(uids) else ''
            gid = gids[i].strip() if i < len(gids) else ''
        # Usage quotidien : partage en écriture par défaut, pas d'option visible.
        mode = 'rw'
        users.append(sftp_SftpUser(i + 1, name, password, uid, gid, host_path, mode))
    return users

def sftp_collect_basic_from_form() -> sftp_SftpBasic:
    restart = request.form.get('basic_restart', 'unless-stopped').strip() or 'unless-stopped'
    return sftp_SftpBasic(container_name=request.form.get('basic_container_name', '').strip(), hostname=request.form.get('basic_hostname', '').strip(), image=request.form.get('basic_image', '').strip(), restart=restart, cpuset=request.form.get('basic_cpuset', '').strip(), icon=request.form.get('basic_icon', '').strip(), data_path=sftp_normalize_path(request.form.get('basic_data_path', '')), data_mode='ro' if request.form.get('basic_data_mode', 'rw').strip().lower() == 'ro' else 'rw', host_port=request.form.get('basic_host_port', '2222').strip() or '2222', container_port=request.form.get('basic_container_port', '22').strip() or '22')

def sftp_collect_network_from_form() -> sftp_SftpNetwork:
    mode = request.form.get('network_mode', 'bridge').strip().lower()
    if mode not in {'bridge', 'lan', 'lan_ip'}:
        mode = 'bridge'
    return sftp_SftpNetwork(mode=mode, network_name=request.form.get('network_name', '').strip(), ip_address=request.form.get('network_ip', '').strip())

def sftp_backup_yaml(path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.basename(path.rstrip('/')) or 'SFTP.yml'
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(backup_dir, f'{base}.{stamp}.bak')
    shutil.copy2(path, dest)
    return dest

def sftp_apply_basic(service: Dict[str, Any], basic: sftp_SftpBasic, env_map: Dict[str, str]) -> None:
    old_image = str(service.get('image', '') or '')
    old_restart = str(service.get('restart', '') or '')
    for key, value in (('container_name', basic.container_name), ('hostname', basic.hostname)):
        if value:
            service[key] = value
        else:
            service.pop(key, None)
    if basic.image:
        service['image'] = sftp_templatize_value(basic.image, old_image, env_map)
    if basic.restart:
        service['restart'] = sftp_templatize_value(basic.restart, old_restart, env_map)
    if basic.cpuset:
        service['cpuset'] = basic.cpuset
    else:
        service.pop('cpuset', None)
    old_icon = sftp_get_label(service, 'net.unraid.docker.icon')
    sftp_set_label(service, 'net.unraid.docker.icon', sftp_templatize_value(basic.icon, old_icon, env_map))
    service['ports'] = [sftp_compose_port(basic.host_port, basic.container_port)]

def sftp_apply_network(data: Dict[str, Any], service: Dict[str, Any], network: sftp_SftpNetwork, env_map: Dict[str, str]) -> None:
    old_networks = service.get('networks')
    old_ip = ''
    old_network_name = network.network_name.strip()
    if isinstance(old_networks, dict) and old_networks:
        old_network_name = str(next(iter(old_networks.keys())))
        cfg = old_networks.get(old_network_name) or {}
        if isinstance(cfg, dict):
            old_ip = str(cfg.get('ipv4_address') or cfg.get('ip_address') or '')
    if network.mode == 'bridge':
        service['network_mode'] = 'bridge'
        service.pop('networks', None)
        return
    service.pop('network_mode', None)
    name = network.network_name.strip()
    if network.mode == 'lan_ip':
        service['networks'] = {name: {'ipv4_address': sftp_templatize_value(network.ip_address.strip(), old_ip, env_map)}}
    else:
        service['networks'] = {name: None}
    top_networks = data.get('networks')
    if not isinstance(top_networks, dict):
        top_networks = {}
        data['networks'] = top_networks
    old_top_name = ''
    if old_network_name and isinstance(top_networks.get(old_network_name), dict):
        old_top_name = str(top_networks[old_network_name].get('name') or '')
    if name not in top_networks or not isinstance(top_networks.get(name), dict):
        top_networks[name] = {'external': True, 'name': sftp_templatize_value(name, old_top_name, env_map)}
    else:
        top_networks[name]['external'] = True
        top_networks[name]['name'] = sftp_templatize_value(name, str(top_networks[name].get('name') or old_top_name), env_map)

def sftp_user_target(user_name: str) -> str:
    return f'/home/{user_name}/Data'

def sftp_update_yaml(conf: Dict[str, str], users: List[sftp_SftpUser], env_values: Dict[str, str]) -> str:
    env_map = sftp_get_env_map(conf)
    data, codec = sftp_load_yaml_file(conf['YAML'], conf)
    service_name = sftp_choose_service(data, conf.get('SERVICE', ''))
    service = data['services'][service_name]
    old_env = sftp_env_to_list(service.get('environment'))
    kept_env: List[str] = []
    old_managed: Dict[str, str] = {}
    editable_keys = {'KEY_VAR'} | sftp_YES_NO_KEYS
    for item in old_env:
        key, value = sftp_split_env_item(item)
        if re.fullmatch('USERS_VAR\\d+', key):
            continue
        if key in editable_keys:
            old_managed[key] = value
            continue
        kept_env.append(item)
    env_out: List[str] = []
    for key in ('KEY_VAR', 'SSH_PUBKEY_AUTH', 'SSH_PASS_AUTH', 'SSH_PERMIT_ROOT', 'SSH_CHALLENGE_AUTH', 'SSH_EMPTY_PASS', 'SSH_USE_PAM', 'SSH_TCP_FORWARD', 'SSH_X11_FORWARD'):
        env_out.append(f"{key}={sftp_env_setting_value(key, env_values.get(key, sftp_DEFAULT_ENV.get(key, '')), old_managed.get(key, ''), env_map)}")
    user_env = [f'USERS_VAR{i}={u.name}:{u.password}:{u.uid}:{u.gid}' for i, u in enumerate(users, 1)]
    service['environment'] = kept_env + env_out + user_env
    old_volumes = service.get('volumes') if isinstance(service.get('volumes'), list) else []
    user_targets = set()
    for raw in old_volumes:
        _source, target, _mode = sftp_parse_volume(raw)
        m = re.fullmatch('/home/([^/]+)/Data', target or '')
        if m:
            user_targets.add(target)
    user_targets.update((sftp_user_target(u.name) for u in users))
    kept_volumes: List[Any] = []
    for raw in old_volumes:
        _source, target, _mode = sftp_parse_volume(raw)
        if target in user_targets:
            continue
        kept_volumes.append(raw)
    for u in users:
        kept_volumes.append(sftp_compose_volume(u.host_path, sftp_user_target(u.name), u.mode))
    service['volumes'] = kept_volumes
    backup = sftp_backup_yaml(conf['YAML'], conf.get('BACKUP_DIR', sftp_DEFAULT_CONFIG['BACKUP_DIR']))
    content = codec.dump(data)
    with open(conf['YAML'], 'w', encoding='utf-8') as handle:
        handle.write(content)
    return backup

def sftp_run_compose_check(conf: Dict[str, str]) -> Tuple[int, str]:
    if not sftp_bool_conf(conf, 'ENABLE_DOCKER_COMPOSE_CHECK', '1'):
        return (0, 'Vérification désactivée dans SFTP.conf.')
    yaml_path = str(conf.get('YAML') or '').strip()
    if not yaml_path or not os.path.exists(yaml_path):
        return (1, sftp_yaml_missing_message(conf, yaml_path))
    cmd = [conf.get('DOCKER_BIN', 'docker') or 'docker', 'compose']
    env_file = sftp_env_file_path(conf)
    if env_file and os.path.exists(env_file):
        cmd.extend(['--env-file', env_file])
    cmd.extend(['-f', yaml_path, 'config'])
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
        output = (completed.stdout or '') + (completed.stderr or '')
        return (completed.returncode, output.strip())
    except FileNotFoundError:
        return (127, 'Commande docker introuvable dans le container Flask System.')
    except subprocess.TimeoutExpired:
        return (124, 'Timeout : docker compose config a pris trop de temps.')
    except Exception as exc:
        return (1, str(exc))


def sftp_get_service_status(conf: Dict[str, str], state: Optional[Dict[str, Any]] = None, error: str = '') -> Dict[str, Any]:
    """Résumé léger pour /services/sftp/main.

    On évite volontairement le `docker compose config` complet ici : la page
    principale doit rester rapide. Elle lit le YAML déjà parsé puis vérifie
    seulement Docker et l'état du container si un nom est connu.
    """
    basic = state.get('basic', sftp_SftpBasic()) if state else sftp_SftpBasic()
    network = state.get('network', sftp_SftpNetwork()) if state else sftp_SftpNetwork()
    service_name = str((state or {}).get('service_name') or conf.get('SERVICE') or 'sftp').strip() or 'sftp'
    container_name = str(basic.container_name or service_name).strip()
    yaml_path = str(conf.get('YAML') or '').strip()
    env_path = sftp_env_file_path(conf)
    data_path = str(basic.data_path or '').strip()

    status: Dict[str, Any] = {
        'service': service_name,
        'container': container_name,
        'active': 'inconnu',
        'container_status': 'inconnu',
        'docker': 'inconnu',
        'docker_message': '',
        'yaml_exists': 'oui' if yaml_path and os.path.exists(yaml_path) else 'non',
        'env_exists': 'oui' if env_path and os.path.exists(env_path) else 'non',
        'data_exists': 'oui' if data_path and os.path.exists(data_path) else ('non' if data_path else 'absent'),
        'users': len(state.get('users', [])) if state else 0,
        'network': network.mode or 'inconnu',
        'network_name': network.network_name or '',
        'network_ip': network.ip_address or '',
        'docker_ip': network.ip_address or '',
        'docker_ip_source': 'yaml' if network.ip_address else '',
        'port': f"{basic.host_port}:{basic.container_port}" if basic.host_port or basic.container_port else '',
        'yaml_path': yaml_path,
        'env_path': env_path,
        'data_path': data_path,
        'compose_check': 'activée' if sftp_bool_conf(conf, 'ENABLE_DOCKER_COMPOSE_CHECK', '1') else 'désactivée',
        'error': error or '',
        'setup_checks': [],
    }

    yml_folder, yml_folder_error = svc_docker_yml_folder_status()
    status['setup_checks'] = [
        {'ok': not bool(yml_folder_error), 'text': f'Module Docker / dossier YAML : {yml_folder or "non configuré"}'},
        {'ok': bool(yaml_path and os.path.exists(yaml_path)), 'text': f'YAML SFTP détecté : {yaml_path or "aucun"}'},
        {'ok': False, 'text': 'Veuillez corriger le dossier YAML Docker ou placer un YAML contenant un vrai service SFTP/SSH.'},
    ]

    rc, out = sftp_docker_cmd(conf, ['version', '--format', '{{.Server.Version}}'], timeout=5)
    if rc == 0:
        status['docker'] = 'ok'
        status['docker_message'] = out.strip()
        if status.get('setup_checks'):
            status['setup_checks'].insert(0, {'ok': True, 'text': f'Docker : OK ({out.strip()})'})
    else:
        status['docker'] = 'non'
        status['docker_message'] = out.strip()
        if status.get('setup_checks'):
            status['setup_checks'].insert(0, {'ok': False, 'text': f'Docker : {out.strip() or "commande indisponible"}'})
        return status

    if error:
        status['active'] = 'erreur'
        status['container_status'] = 'erreur'
        return status

    if container_name:
        rc, out = sftp_docker_cmd(conf, ['inspect', container_name], timeout=8)
        container_data: Dict[str, Any] = {}
        if rc == 0 and out.strip():
            try:
                parsed = json.loads(out)
                if isinstance(parsed, list) and parsed:
                    container_data = parsed[0] if isinstance(parsed[0], dict) else {}
            except Exception:
                container_data = {}

        if container_data:
            raw = str(((container_data.get('State') or {}).get('Status') or '')).strip()
            status['container_status'] = raw or 'inconnu'
            status['active'] = 'actif' if raw == 'running' else 'arrêté'
            docker_ip = sftp_container_ip_from_inspect(container_data, network.network_name, network.ip_address)
            if not docker_ip:
                docker_ip = sftp_container_ip_from_docker_cli(conf, container_name)
            status['docker_ip'] = docker_ip or status['docker_ip'] or '-'
            status['docker_ip_source'] = 'docker' if docker_ip else status.get('docker_ip_source', '')
        else:
            status['container_status'] = 'absent'
            status['active'] = 'arrêté'
            status['docker_ip'] = sftp_container_ip_from_docker_cli(conf, container_name) or status['docker_ip'] or '-'
    return status



def _render_sftp():
    forced_subtab = str(getattr(g, 'services_forced_subtab', '') or '').strip().lower()
    if forced_subtab == 'config':
        return services_redirect('sftp', subtab='main')

    conf = sftp_get_config()
    error = ''
    state: Optional[Dict[str, Any]] = None
    detected_networks = sftp_detect_docker_networks(conf)
    try:
        state = sftp_current_state(conf)
    except Exception as exc:
        error = str(exc)
    basic = state.get('basic', sftp_SftpBasic()) if state else sftp_SftpBasic()
    network = state.get('network', sftp_SftpNetwork()) if state else sftp_SftpNetwork()
    if network.mode in {'lan', 'lan_ip'} and (not network.network_name) and detected_networks:
        network.network_name = detected_networks[0]['name']
    env_values = state.get('env', sftp_DEFAULT_ENV.copy()) if state else sftp_DEFAULT_ENV.copy()
    users = state.get('users', []) if state else []
    status = sftp_get_service_status(conf, state, error)
    return render_template('services_sftp.html', conf=conf, config_file=sftp_CONFIG_FILE, error=error, state=state, basic=basic, network=network, detected_networks=detected_networks, env_values=env_values, users=users, linux_users=sftp_list_linux_users(users), allowed_roots=sftp_allowed_roots(conf), env_file=state.get('env_file', sftp_env_file_path(conf)) if state else sftp_env_file_path(conf), active_subtab=services_requested_subtab({'main', 'users', 'ssh'}, 'main'), status=status, service_active='sftp')

@services_bp.route('/services/sftp/save', methods=['POST'])
def sftp_sftp_save():
    conf = sftp_get_config()
    try:
        state = sftp_current_state(conf)
        users = sftp_collect_users_from_form(state['users'])
        env_values = sftp_collect_env_from_form(state['env'])
        roots = sftp_allowed_roots(conf)
        errors = sftp_validate_users(users, roots)
        if errors:
            for err in errors:
                flash('❌ ' + err, 'error')
            return services_redirect('sftp', subtab=str(request.form.get('return_subtab') or 'users').strip().lower())
        backup = sftp_update_yaml(conf, users, env_values)
        rc, output = sftp_run_compose_check(conf)
        if rc == 0:
            flash(f'✅ YAML SFTP sauvegardé. Backup : {backup}', 'success')
        else:
            flash(f'⚠️ YAML sauvegardé, mais docker compose config signale une erreur :\n{output}', 'error')
    except Exception as exc:
        flash(f'❌ Erreur sauvegarde SFTP : {exc}', 'error')
    return services_redirect('sftp', subtab=str(request.form.get('return_subtab') or 'users').strip().lower())

@services_bp.route('/services/sftp/check', methods=['POST'])
def sftp_sftp_check():
    conf = sftp_get_config()
    rc, output = sftp_run_compose_check(conf)
    return jsonify({'ok': rc == 0, 'code': rc, 'output': output or 'OK'})

@services_bp.route('/services/sftp/api/browse', methods=['GET'])
def sftp_sftp_browse():
    conf = sftp_get_config()
    roots = sftp_allowed_roots(conf)
    requested = sftp_normalize_path(request.args.get('path') or roots[0])
    if not sftp_is_under_allowed_root(requested, roots):
        requested = roots[0]
    real = os.path.realpath(requested)
    if not os.path.isdir(real):
        return (jsonify({'ok': False, 'path': real, 'error': 'Dossier introuvable ou non accessible.', 'items': []}), 404)
    items = []
    try:
        if real not in roots:
            parent = os.path.dirname(real.rstrip('/')) or '/'
            if sftp_is_under_allowed_root(parent, roots):
                items.append({'name': '..', 'path': parent, 'type': 'parent'})
        for name in sorted(os.listdir(real), key=str.lower):
            path = os.path.join(real, name)
            if os.path.isdir(path):
                items.append({'name': name, 'path': path, 'type': 'dir'})
    except PermissionError:
        return (jsonify({'ok': False, 'path': real, 'error': 'Permission refusée.', 'items': items}), 403)
    except Exception as exc:
        return (jsonify({'ok': False, 'path': real, 'error': str(exc), 'items': items}), 500)
    return jsonify({'ok': True, 'path': real, 'roots': roots, 'items': items})

# ============================================================
# ARCHIVE MERGED MODULE
# ============================================================
