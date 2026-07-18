@dataclass
class pro_ProftpdUser:
    index: int
    name: str
    password: str
    uid: str
    gid: str
    host_path: str = ''
    mode: str = 'rw'

@dataclass
class pro_ProftpdShare:
    index: int
    name: str
    host_path: str
    container_path: str
    mode: str = 'rw'

@dataclass
class pro_ProftpdBasic:
    container_name: str = ''
    hostname: str = ''
    image: str = ''
    restart: str = 'unless-stopped'
    cpuset: str = ''
    icon: str = ''
    data_path: str = ''
    data_mode: str = 'rw'
    ftp_host_port: str = '21'
    ftp_container_port: str = '21'
    passive_host_start: str = '30000'
    passive_host_end: str = '30100'
    passive_container_start: str = '30000'
    passive_container_end: str = '30100'

@dataclass
class pro_ProftpdNetwork:
    mode: str = 'lan_ip'
    network_name: str = ''
    ip_address: str = ''

class pro_YamlCodec:

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

def pro_read_kv_file(path: str) -> Dict[str, str]:
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

def pro_write_kv_file(path: str, data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    order = ['YAML', 'SERVICE', 'BROWSE_ROOTS', 'BACKUP_DIR', 'DOCKER_BIN', 'ENABLE_DOCKER_COMPOSE_CHECK']
    lines = ['# Configuration du module Flask ProFTPD', '# YAML = chemin complet vers le docker-compose/yml du Docker Proftpd']
    for key in order:
        value = data.get(key, pro_DEFAULT_CONFIG.get(key, ''))
        lines.append(f'{key}={value}')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip() + '\n')

def pro_get_config() -> Dict[str, str]:
    conf = pro_DEFAULT_CONFIG.copy()
    conf.update(pro_read_kv_file(pro_CONFIG_FILE))
    if not os.path.exists(pro_CONFIG_FILE):
        pro_write_kv_file(pro_CONFIG_FILE, conf)
    return conf
pro_ENV_VAR_RE = re.compile('\\$\\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\\}')

def pro_strip_env_quotes(value: str) -> str:
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and (value[0] in {"'", '"'}):
        return value[1:-1]
    return value

def pro_env_file_path(conf: Dict[str, str]) -> str:
    """Le .env vient du même dossier YAML que le module Docker."""
    return svc_env_file_from_docker_yaml(conf)

def pro_read_env_file(path: str) -> Dict[str, str]:
    """
    Lecture .env Docker Compose case-sensitive.
    Important : ${ip_proftpd}, ${docker_type}, ${logo_path}, etc. gardent leur casse.
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
                data[key] = pro_strip_env_quotes(value)
    return data

def pro_get_env_map(conf: Dict[str, str]) -> Dict[str, str]:
    return pro_read_env_file(pro_env_file_path(conf))

def pro_resolve_env_string(value: str, env_map: Dict[str, str]) -> str:
    text = str(value or '')

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(3)
        if key in env_map:
            return env_map[key]
        return default or ''
    return pro_ENV_VAR_RE.sub(repl, text)

def pro_resolve_env_tree(value: Any, env_map: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return pro_resolve_env_string(value, env_map)
    if isinstance(value, list):
        return [pro_resolve_env_tree(item, env_map) for item in value]
    if isinstance(value, dict):
        return {key: pro_resolve_env_tree(item, env_map) for key, item in value.items()}
    return value

def pro_env_keys_in(value: Any) -> List[str]:
    return [m.group(1) for m in pro_ENV_VAR_RE.finditer(str(value or ''))]

def pro_env_prefix_candidates(env_map: Dict[str, str]) -> List[Tuple[str, str]]:
    preferred: List[Tuple[str, str]] = []
    normal: List[Tuple[str, str]] = []
    for key, value in env_map.items():
        if not value:
            continue
        item = (key, value)
        if key.startswith('PATH_') or key in {'REGISTRY', 'BR0_NAME', 'logo_path'} or key.startswith('ip_'):
            preferred.append(item)
        else:
            normal.append(item)
    return sorted(preferred, key=lambda x: len(x[1]), reverse=True) + sorted(normal, key=lambda x: len(x[1]), reverse=True)

def pro_replace_prefix_with_env(value: str, key: str, env_value: str) -> str:
    if not env_value:
        return value
    if value == env_value:
        return '${' + key + '}'
    if value.startswith(env_value.rstrip('/') + '/'):
        return '${' + key + '}' + value[len(env_value.rstrip('/')):]
    if value.startswith(env_value + '/'):
        return '${' + key + '}' + value[len(env_value):]
    return value

def pro_templatize_value(value: str, old_raw: str='', env_map: Optional[Dict[str, str]]=None) -> str:
    """
    Convertit la valeur affichée dans le formulaire vers une valeur YAML.
    Si le YAML contenait déjà ${VAR}, on la conserve quand la valeur résolue est identique.
    Sinon on remplace seulement par une variable existante du .env si le préfixe correspond.
    """
    env_map = env_map or {}
    value = str(value or '').strip()
    old_raw = str(old_raw or '').strip()
    if old_raw and pro_resolve_env_string(old_raw, env_map) == value:
        return old_raw
    for key in pro_env_keys_in(old_raw):
        if key in env_map:
            replaced = pro_replace_prefix_with_env(value, key, env_map[key])
            if replaced != value:
                value = replaced
    for key, env_value in pro_env_prefix_candidates(env_map):
        replaced = pro_replace_prefix_with_env(value, key, env_value)
        if replaced != value:
            value = replaced
            break
    return value

def pro_env_setting_value(key: str, value: str, old_raw: str, env_map: Dict[str, str]) -> str:
    """
    Garde ${KEY} si le champ correspond à une clé existante du .env.
    N'invente aucune nouvelle clé.
    """
    value = str(value or '').strip()
    old_raw = str(old_raw or '').strip()
    if old_raw and pro_resolve_env_string(old_raw, env_map) == value:
        return old_raw
    if key in env_map and env_map[key] == value:
        return '${' + key + '}'
    return pro_templatize_value(value, old_raw, env_map)

def pro_raw_env_value(env_items: List[str], key_name: str, default: str='') -> str:
    for item in env_items:
        key, value = pro_split_env_item(item)
        if key == key_name:
            return value
    return default

def pro_normalize_path(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    return os.path.normpath(value.replace('\\', '/'))

def pro_bool_conf(conf: Dict[str, str], key: str, default: str='0') -> bool:
    return str(conf.get(key, default)).strip().lower() in {'1', 'true', 'yes', 'on'}

def pro_allowed_roots(conf: Dict[str, str]) -> List[str]:
    roots = []
    for raw in conf.get('BROWSE_ROOTS', '/mnt/user').split(','):
        raw = pro_normalize_path(raw)
        if raw:
            roots.append(os.path.realpath(raw))
    return roots or ['/mnt/user']

def pro_is_under_allowed_root(path: str, roots: List[str]) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    for root in roots:
        if root == '/':
            return True
        if real == root or real.startswith(root.rstrip('/') + '/'):
            return True
    return False

def pro_load_yaml_file(path: str) -> Tuple[Any, pro_YamlCodec]:
    codec = pro_YamlCodec()
    if not os.path.exists(path):
        raise FileNotFoundError(svc_docker_yaml_error('ProFTPD', 'proftpd.yml', path))
    with open(path, 'r', encoding='utf-8') as handle:
        return (codec.load(handle.read()) or {}, codec)

def pro_choose_service(data: Any, configured_service: str='') -> str:
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict) or (not data['services']):
        raise ValueError('Le YAML ne contient pas de bloc services valide.')
    services = data['services']
    configured_service = (configured_service or '').strip()
    if configured_service and configured_service in services:
        return configured_service
    for name in services.keys():
        lowered = str(name).lower()
        if 'proftpd' in lowered or lowered in {'ftp', 'ftpd'}:
            return str(name)
    return str(next(iter(services.keys())))

def pro_env_to_list(env: Any) -> List[str]:
    if env is None:
        return []
    if isinstance(env, list):
        return [str(item) for item in env]
    if isinstance(env, dict):
        return [f'{key}={value}' for key, value in env.items()]
    return []

def pro_split_env_item(item: str) -> Tuple[str, str]:
    if '=' not in item:
        return (item.strip(), '')
    key, value = item.split('=', 1)
    return (key.strip(), value.strip())

def pro_env_dict(env_items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = pro_DEFAULT_ENV.copy()
    for item in env_items:
        key, value = pro_split_env_item(item)
        if key and (not re.fullmatch('USERS_VAR\\d+', key)):
            out[key] = value
    return out

def pro_parse_volume(raw: Any) -> Tuple[str, str, str]:
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

def pro_compose_volume(source: str, target: str, mode: str) -> str:
    mode = 'ro' if mode == 'ro' else 'rw'
    return f'{source}:{target}:{mode}'

def pro_user_target(user_name: str) -> str:
    safe = re.sub('[^a-zA-Z0-9_.-]+', '_', user_name or 'user').strip('_') or 'user'
    return f'/data/home/{safe}/Data'

def pro_parse_users(env_items: List[str], volumes: List[Any]) -> List[pro_ProftpdUser]:
    volume_by_user: Dict[str, Tuple[str, str]] = {}
    for raw in volumes:
        source, target, mode = pro_parse_volume(raw)
        m = re.fullmatch('/data/home/([^/]+)/Data', target or '')
        if m:
            volume_by_user[m.group(1)] = (source, mode if mode in {'ro', 'rw'} else 'rw')
    out: List[pro_ProftpdUser] = []
    for item in env_items:
        key, value = pro_split_env_item(item)
        m = re.fullmatch('USERS_VAR(\\d+)', key)
        if not m:
            continue
        parts = value.split(':')
        while len(parts) < 4:
            parts.append('')
        user = pro_ProftpdUser(int(m.group(1)), parts[0], parts[1], parts[2], parts[3])
        hp, mode = volume_by_user.get(user.name, ('', 'rw'))
        user.host_path = hp
        user.mode = mode
        out.append(user)
    return sorted(out, key=lambda x: x.index)

def pro_parse_shares(volumes: List[Any]) -> List[pro_ProftpdShare]:
    out: List[pro_ProftpdShare] = []
    idx = 1
    for raw in volumes:
        source, target, mode = pro_parse_volume(raw)
        if not target or target == '/data' or re.fullmatch('/data/home/[^/]+/Data', target):
            continue
        name = os.path.basename(target.rstrip('/')) or target.strip('/').replace('/', '_') or f'partage{idx}'
        out.append(pro_ProftpdShare(idx, name, source, target, mode if mode in {'ro', 'rw'} else 'rw'))
        idx += 1
    return out

def pro__sexagesimal_to_port(value: int) -> Tuple[str, str]:
    if value > 59:
        return (str(value // 60), str(value % 60))
    return (str(value), str(value))

def pro_parse_port_pair(raw: Any) -> Tuple[str, str]:
    if isinstance(raw, int):
        return pro__sexagesimal_to_port(raw)
    if isinstance(raw, dict):
        published = str(raw.get('published') or raw.get('host_port') or raw.get('published_port') or '')
        target = str(raw.get('target') or raw.get('container_port') or '')
        return (published, target)
    text = str(raw or '').strip().strip('"').strip("'")
    if not text:
        return ('', '')
    text = text.split('/', 1)[0]
    parts = text.split(':')
    if len(parts) >= 2:
        return (parts[-2].strip(), parts[-1].strip())
    return ('', parts[0].strip())

def pro_parse_ports(ports: Any) -> Tuple[str, str, str, str, str, str]:
    ftp_h, ftp_c = ('21', '21')
    phs, phe, pcs, pce = ('30000', '30100', '30000', '30100')
    if not isinstance(ports, list):
        return (ftp_h, ftp_c, phs, phe, pcs, pce)
    for raw in ports:
        host, cont = pro_parse_port_pair(raw)
        if not host and (not cont):
            continue
        if '-' in host or '-' in cont:
            h1, h2 = (host.split('-', 1) + [''])[:2]
            c1, c2 = (cont.split('-', 1) + [''])[:2]
            phs, phe = (h1 or phs, h2 or phe)
            pcs, pce = (c1 or pcs, c2 or pce)
        elif cont == '21' or host == '21':
            ftp_h, ftp_c = (host or ftp_h, cont or ftp_c)
    return (ftp_h, ftp_c, phs, phe, pcs, pce)

def pro_compose_port(host_port: str, container_port: str) -> str:
    return f"{str(host_port or '').strip()}:{str(container_port or '').strip()}"

def pro_compose_port_range(hs: str, he: str, cs: str, ce: str) -> str:
    return f'{hs}-{he}:{cs}-{ce}'

def pro_get_label(service: Dict[str, Any], key: str) -> str:
    labels = service.get('labels')
    if isinstance(labels, dict):
        return str(labels.get(key, '') or '')
    if isinstance(labels, list):
        for item in labels:
            k, v = pro_split_env_item(str(item))
            if k == key:
                return v
    return ''

def pro_set_label(service: Dict[str, Any], key: str, value: str) -> None:
    labels = service.get('labels')
    if not value:
        if isinstance(labels, dict):
            labels.pop(key, None)
        elif isinstance(labels, list):
            service['labels'] = [x for x in labels if pro_split_env_item(str(x))[0] != key]
        return
    if isinstance(labels, dict):
        labels[key] = value
    elif isinstance(labels, list):
        out = []
        done = False
        for item in labels:
            k, _v = pro_split_env_item(str(item))
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

def pro_parse_basic(service: Dict[str, Any], volumes: List[Any]) -> pro_ProftpdBasic:
    data_path = ''
    data_mode = 'rw'
    for raw in volumes:
        source, target, mode = pro_parse_volume(raw)
        if target == '/data':
            data_path = source
            data_mode = mode if mode in {'ro', 'rw'} else 'rw'
            break
    ftp_h, ftp_c, phs, phe, pcs, pce = pro_parse_ports(service.get('ports'))
    return pro_ProftpdBasic(container_name=str(service.get('container_name', '') or ''), hostname=str(service.get('hostname', '') or ''), image=str(service.get('image', '') or ''), restart=str(service.get('restart', 'unless-stopped') or 'unless-stopped'), cpuset=str(service.get('cpuset', '') or ''), icon=pro_get_label(service, 'net.unraid.docker.icon'), data_path=data_path, data_mode=data_mode, ftp_host_port=ftp_h or '21', ftp_container_port=ftp_c or '21', passive_host_start=phs or '30000', passive_host_end=phe or '30100', passive_container_start=pcs or '30000', passive_container_end=pce or '30100')

def pro_parse_network(service: Dict[str, Any]) -> pro_ProftpdNetwork:
    mode = str(service.get('network_mode', '') or '').strip().lower()
    if mode == 'bridge':
        return pro_ProftpdNetwork('bridge', '', '')
    if mode == 'host':
        return pro_ProftpdNetwork('bridge', '', '')
    networks = service.get('networks')
    if isinstance(networks, dict) and networks:
        name = str(next(iter(networks.keys())))
        cfg = networks.get(name) or {}
        ip = ''
        if isinstance(cfg, dict):
            ip = str(cfg.get('ipv4_address') or cfg.get('ip_address') or '')
        return pro_ProftpdNetwork('lan_ip' if ip else 'lan', name, ip)
    if isinstance(networks, list) and networks:
        return pro_ProftpdNetwork('lan', str(networks[0]), '')
    return pro_ProftpdNetwork('bridge', '', '')

def pro_docker_cmd(conf: Dict[str, str], args: List[str], timeout: int=8) -> Tuple[int, str]:
    cmd = [conf.get('DOCKER_BIN', 'docker') or 'docker'] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.returncode, ((p.stdout or '') + (p.stderr or '')).strip())
    except Exception as exc:
        return (1, str(exc))

def pro_detect_docker_networks(conf: Dict[str, str]) -> List[Dict[str, str]]:
    rc, out = pro_docker_cmd(conf, ['network', 'ls', '--format', '{{.Name}}'], timeout=8)
    if rc != 0:
        return []
    result: List[Dict[str, str]] = []
    for name in [x.strip() for x in out.splitlines() if x.strip()]:
        if name in {'host', 'none', 'bridge'}:
            continue
        rc2, raw = pro_docker_cmd(conf, ['network', 'inspect', name], timeout=8)
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

def pro_current_state(conf: Dict[str, str]) -> Dict[str, Any]:
    if conf.get('_DOCKER_YAML_ERROR'):
        raise FileNotFoundError(conf['_DOCKER_YAML_ERROR'])
    raw_data, _codec = pro_load_yaml_file(conf['YAML'])
    env_map = pro_get_env_map(conf)
    data = pro_resolve_env_tree(raw_data, env_map)
    service_name = pro_choose_service(raw_data, conf.get('SERVICE', ''))
    raw_service = raw_data['services'][service_name]
    service = data['services'][service_name]
    env_items = pro_env_to_list(service.get('environment'))
    raw_env_items = pro_env_to_list(raw_service.get('environment'))
    volumes = service.get('volumes') if isinstance(service.get('volumes'), list) else []
    raw_volumes = raw_service.get('volumes') if isinstance(raw_service.get('volumes'), list) else []
    basic = pro_parse_basic(service, volumes)
    network = pro_parse_network(service)
    return {'data': data, 'raw_data': raw_data, 'service_name': service_name, 'service': service, 'raw_service': raw_service, 'basic': basic, 'network': network, 'env': pro_env_dict(env_items), 'users': pro_parse_users(env_items, volumes), 'shares': pro_parse_shares(volumes), 'env_items': env_items, 'raw_env_items': raw_env_items, 'volumes': volumes, 'raw_volumes': raw_volumes, 'env_file': pro_env_file_path(conf)}

def pro_validate_basic(basic: pro_ProftpdBasic, roots: List[str]) -> List[str]:
    errors: List[str] = []
    if not basic.data_path.startswith('/'):
        errors.append(f'Volume /data invalide : {basic.data_path}')
    elif not pro_is_under_allowed_root(basic.data_path, roots):
        errors.append(f'Volume /data refusé : hors BROWSE_ROOTS ({basic.data_path}).')
    for label, value in (('Port FTP hôte', basic.ftp_host_port), ('Port FTP container', basic.ftp_container_port), ('Début ports passifs hôte', basic.passive_host_start), ('Fin ports passifs hôte', basic.passive_host_end), ('Début ports passifs container', basic.passive_container_start), ('Fin ports passifs container', basic.passive_container_end)):
        if not str(value).isdigit():
            errors.append(f'{label} invalide : {value}')
        elif not 1 <= int(value) <= 65535:
            errors.append(f'{label} hors plage : {value}')
    if basic.passive_host_start.isdigit() and basic.passive_host_end.isdigit() and (int(basic.passive_host_start) > int(basic.passive_host_end)):
        errors.append('Plage passive hôte inversée.')
    if basic.passive_container_start.isdigit() and basic.passive_container_end.isdigit() and (int(basic.passive_container_start) > int(basic.passive_container_end)):
        errors.append('Plage passive container inversée.')
    return errors

def pro_validate_users(users: List[pro_ProftpdUser], roots: List[str]) -> List[str]:
    errors: List[str] = []
    seen = set()
    for user in users:
        if not user.name:
            errors.append('Un utilisateur a un nom vide.')
            continue
        if not pro_USER_RE.fullmatch(user.name):
            errors.append(f'Utilisateur invalide : {user.name}. Utilise minuscules, chiffres, _ et -.')
        if user.name in seen:
            errors.append(f'Utilisateur en double : {user.name}')
        seen.add(user.name)
        if not user.password:
            errors.append(f'Mot de passe manquant pour {user.name}.')
        if ':' in user.password:
            errors.append(f"Mot de passe refusé pour {user.name} : le caractère ':' est interdit avec le format USERS_VAR.")
        if not user.uid.isdigit():
            errors.append(f'UID invalide pour {user.name} : {user.uid}')
        if not user.gid.isdigit():
            errors.append(f'GID invalide pour {user.name} : {user.gid}')
        if user.host_path:
            if not user.host_path.startswith('/'):
                errors.append(f'Chemin hôte invalide pour {user.name} : {user.host_path}')
            elif not pro_is_under_allowed_root(user.host_path, roots):
                errors.append(f'Chemin hôte refusé pour {user.name} : hors BROWSE_ROOTS.')
        if user.mode not in {'rw', 'ro'}:
            errors.append(f'Mode invalide pour {user.name} : {user.mode}')
    return errors

def pro_validate_shares(shares: List[pro_ProftpdShare], roots: List[str]) -> List[str]:
    errors: List[str] = []
    seen_targets = set()
    seen_names = set()
    for share in shares:
        if not share.name:
            errors.append('Un volume supplémentaire a un nom vide.')
        elif not pro_SHARE_RE.fullmatch(share.name):
            errors.append(f"Nom de volume invalide : {share.name}. Pas de '/', '\\', ':' ou caractère nul.")
        if share.name.lower() in seen_names:
            errors.append(f'Nom de volume en double : {share.name}')
        seen_names.add(share.name.lower())
        if not share.host_path.startswith('/'):
            errors.append(f'Chemin hôte invalide pour {share.name} : {share.host_path}')
        elif not pro_is_under_allowed_root(share.host_path, roots):
            errors.append(f'Chemin hôte refusé pour {share.name} : hors BROWSE_ROOTS.')
        if not share.container_path.startswith('/'):
            errors.append(f'Chemin container invalide pour {share.name} : {share.container_path}')
        if share.container_path in pro_RESERVED_TARGETS:
            errors.append(f'Chemin container réservé pour {share.name} : {share.container_path}')
        if re.fullmatch('/data/home/[^/]+/Data', share.container_path):
            errors.append(f'Chemin réservé aux dossiers utilisateurs pour {share.name} : {share.container_path}')
        if share.container_path in seen_targets:
            errors.append(f'Chemin container en double : {share.container_path}')
        seen_targets.add(share.container_path)
        if share.mode not in {'rw', 'ro'}:
            errors.append(f'Mode invalide pour {share.name} : {share.mode}')
    return errors

def pro_validate_network(network: pro_ProftpdNetwork, detected: List[Dict[str, str]]) -> List[str]:
    errors: List[str] = []
    if network.mode not in {'bridge', 'lan', 'lan_ip'}:
        errors.append('Type de réseau invalide. Utilise bridge, LAN ou LAN IP fixe.')
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

def pro_collect_users_from_form(existing_users: List[pro_ProftpdUser]) -> List[pro_ProftpdUser]:
    old_passwords = {u.name: u.password for u in existing_users}
    names = request.form.getlist('user_name[]')
    original = request.form.getlist('user_original[]')
    passwords = request.form.getlist('user_password[]')
    uids = request.form.getlist('user_uid[]')
    gids = request.form.getlist('user_gid[]')
    host_paths = request.form.getlist('user_host_path[]')
    modes = request.form.getlist('user_mode[]')
    users: List[pro_ProftpdUser] = []
    count = max(len(names), len(passwords), len(uids), len(gids), len(host_paths), len(modes), len(original))
    for i in range(count):
        name = names[i].strip() if i < len(names) else ''
        old_name = original[i].strip() if i < len(original) else name
        password = passwords[i] if i < len(passwords) else ''
        uid = uids[i].strip() if i < len(uids) else ''
        gid = gids[i].strip() if i < len(gids) else ''
        host_path = pro_normalize_path(host_paths[i]) if i < len(host_paths) else ''
        mode = modes[i].strip().lower() if i < len(modes) else 'rw'
        mode = 'ro' if mode == 'ro' else 'rw'
        if not any([name, password, uid, gid, host_path]):
            continue
        if not password and old_name in old_passwords:
            password = old_passwords[old_name]
        users.append(pro_ProftpdUser(i + 1, name, password, uid, gid, host_path, mode))
    return users

def pro_collect_shares_from_form() -> Tuple[List[pro_ProftpdShare], List[str]]:
    names = request.form.getlist('share_name[]')
    host_paths = request.form.getlist('share_host_path[]')
    container_paths = request.form.getlist('share_container_path[]')
    modes = request.form.getlist('share_mode[]')
    original_targets = request.form.getlist('share_original_container[]')
    shares: List[pro_ProftpdShare] = []
    count = max(len(names), len(host_paths), len(container_paths), len(modes), len(original_targets))
    for i in range(count):
        name = names[i].strip() if i < len(names) else ''
        host_path = pro_normalize_path(host_paths[i]) if i < len(host_paths) else ''
        container_path = pro_normalize_path(container_paths[i]) if i < len(container_paths) else ''
        mode = modes[i].strip().lower() if i < len(modes) else 'rw'
        mode = 'ro' if mode == 'ro' else 'rw'
        if not any([name, host_path, container_path]):
            continue
        if container_path and (not container_path.startswith('/')):
            container_path = '/' + container_path
        shares.append(pro_ProftpdShare(i + 1, name, host_path, container_path, mode))
    return (shares, [x.strip() for x in original_targets if x.strip()])

def pro_collect_basic_from_form() -> pro_ProftpdBasic:
    restart = request.form.get('basic_restart', 'unless-stopped').strip() or 'unless-stopped'
    if restart not in {'no', 'always', 'unless-stopped', 'on-failure'}:
        restart = 'unless-stopped'
    return pro_ProftpdBasic(container_name=request.form.get('basic_container_name', '').strip(), hostname=request.form.get('basic_hostname', '').strip(), image=request.form.get('basic_image', '').strip(), restart=restart, cpuset=request.form.get('basic_cpuset', '').strip(), icon=request.form.get('basic_icon', '').strip(), data_path=pro_normalize_path(request.form.get('basic_data_path', '')), data_mode='ro' if request.form.get('basic_data_mode', 'rw').strip().lower() == 'ro' else 'rw', ftp_host_port=request.form.get('basic_ftp_host_port', '21').strip() or '21', ftp_container_port=request.form.get('basic_ftp_container_port', '21').strip() or '21', passive_host_start=request.form.get('basic_passive_host_start', '30000').strip() or '30000', passive_host_end=request.form.get('basic_passive_host_end', '30100').strip() or '30100', passive_container_start=request.form.get('basic_passive_container_start', '30000').strip() or '30000', passive_container_end=request.form.get('basic_passive_container_end', '30100').strip() or '30100')

def pro_collect_network_from_form() -> pro_ProftpdNetwork:
    mode = request.form.get('network_mode', 'bridge').strip().lower()
    if mode not in {'bridge', 'lan', 'lan_ip'}:
        mode = 'bridge'
    return pro_ProftpdNetwork(mode=mode, network_name=request.form.get('network_name', '').strip(), ip_address=request.form.get('network_ip', '').strip())

def pro_collect_env_from_form(existing: Dict[str, str]) -> Dict[str, str]:
    env = existing.copy()
    env['TZ'] = request.form.get('env_TZ', existing.get('TZ', pro_DEFAULT_ENV['TZ'])).strip() or pro_DEFAULT_ENV['TZ']
    return env

def pro_backup_yaml(path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.basename(path.rstrip('/')) or 'proftpd.yml'
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(backup_dir, f'{base}.{stamp}.bak')
    shutil.copy2(path, dest)
    return dest

def pro_apply_basic(service: Dict[str, Any], basic: pro_ProftpdBasic, network: pro_ProftpdNetwork, old_basic: pro_ProftpdBasic, env_map: Dict[str, str]) -> None:
    simple_fields = (('container_name', basic.container_name), ('hostname', basic.hostname))
    for key, value in simple_fields:
        if value:
            service[key] = value
        else:
            service.pop(key, None)
    if basic.image:
        service['image'] = pro_templatize_value(basic.image, old_basic.image, env_map)
    else:
        service.pop('image', None)
    if basic.restart:
        service['restart'] = pro_env_setting_value('RESTART_POLICY', basic.restart, old_basic.restart, env_map)
    else:
        service.pop('restart', None)
    if basic.cpuset:
        service['cpuset'] = basic.cpuset
    else:
        service.pop('cpuset', None)
    pro_set_label(service, 'net.unraid.docker.icon', pro_templatize_value(basic.icon, old_basic.icon, env_map))
    if network.mode == 'bridge':
        service['ports'] = [pro_compose_port(basic.ftp_host_port, basic.ftp_container_port), pro_compose_port_range(basic.passive_host_start, basic.passive_host_end, basic.passive_container_start, basic.passive_container_end)]
    else:
        service.pop('ports', None)

def pro_apply_network(data: Dict[str, Any], service: Dict[str, Any], network: pro_ProftpdNetwork, old_network: pro_ProftpdNetwork, env_map: Dict[str, str]) -> None:
    if network.mode == 'bridge':
        service['network_mode'] = 'bridge'
        service.pop('networks', None)
        return
    service.pop('network_mode', None)
    name = network.network_name.strip()
    ip_value = network.ip_address.strip()
    if network.mode == 'lan_ip':
        old_ip = old_network.ip_address
        ip_yaml = pro_env_setting_value('ip_proftpd', ip_value, old_ip, env_map)
        service['networks'] = {name: {'ipv4_address': ip_yaml}}
    else:
        service['networks'] = {name: None}
    top_networks = data.get('networks')
    if not isinstance(top_networks, dict):
        top_networks = {}
        data['networks'] = top_networks
    network_real_name = name
    if env_map.get('BR0_NAME') == name:
        network_real_name = '${BR0_NAME}'
    if name not in top_networks or not isinstance(top_networks.get(name), dict):
        top_networks[name] = {'external': True, 'name': network_real_name}
    else:
        top_networks[name]['external'] = True
        top_networks[name]['name'] = network_real_name

def pro_update_yaml(conf: Dict[str, str], users: List[pro_ProftpdUser], shares: List[pro_ProftpdShare], original_share_targets: List[str]) -> str:
    data, codec = pro_load_yaml_file(conf['YAML'])
    service_name = pro_choose_service(data, conf.get('SERVICE', ''))
    service = data['services'][service_name]
    old_env = pro_env_to_list(service.get('environment'))
    kept_env: List[str] = []
    for item in old_env:
        key, _value = pro_split_env_item(item)
        if re.fullmatch('USERS_VAR\\d+', key):
            continue
        kept_env.append(item)
    user_env = [f'USERS_VAR{i}={u.name}:{u.password}:{u.uid}:{u.gid}' for i, u in enumerate(users, 1)]
    service['environment'] = kept_env + user_env
    old_volumes = service.get('volumes') if isinstance(service.get('volumes'), list) else []
    old_user_targets = set()
    for raw in old_volumes:
        _source, target, _mode = pro_parse_volume(raw)
        if re.fullmatch('/data/home/[^/]+/Data', target or ''):
            old_user_targets.add(target)
    new_user_targets = {pro_user_target(u.name) for u in users if u.host_path}
    remove_targets = old_user_targets | new_user_targets | set(original_share_targets) | {s.container_path for s in shares}
    kept_volumes: List[Any] = []
    for raw in old_volumes:
        _source, target, _mode = pro_parse_volume(raw)
        if target and target in remove_targets:
            continue
        kept_volumes.append(raw)
    for u in users:
        if u.host_path:
            kept_volumes.append(pro_compose_volume(u.host_path, pro_user_target(u.name), u.mode))
    for s in shares:
        kept_volumes.append(pro_compose_volume(s.host_path, s.container_path, s.mode))
    service['volumes'] = kept_volumes
    backup = pro_backup_yaml(conf['YAML'], conf.get('BACKUP_DIR', pro_DEFAULT_CONFIG['BACKUP_DIR']))
    content = codec.dump(data)
    with open(conf['YAML'], 'w', encoding='utf-8') as handle:
        handle.write(content)
    return backup

def pro_run_compose_check(conf: Dict[str, str]) -> Tuple[int, str]:
    if not pro_bool_conf(conf, 'ENABLE_DOCKER_COMPOSE_CHECK', '1'):
        return (0, 'Vérification désactivée dans Proftpd.conf.')
    cmd = [conf.get('DOCKER_BIN', 'docker') or 'docker', 'compose']
    env_path = pro_env_file_path(conf)
    if os.path.exists(env_path):
        cmd.extend(['--env-file', env_path])
    cmd.extend(['-f', conf['YAML'], 'config'])
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


def pro_run_cmd(cmd: List[str], timeout: int=30) -> Tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = ((completed.stdout or '') + (completed.stderr or '')).strip()
        return (completed.returncode, output)
    except FileNotFoundError:
        return (127, f'Commande introuvable : {cmd[0]}')
    except subprocess.TimeoutExpired:
        return (124, 'Timeout : ' + ' '.join(cmd))
    except Exception as exc:
        return (1, str(exc))


def pro_service_name(conf: Dict[str, str]) -> str:
    return (conf.get('SERVICE_NAME') or conf.get('SERVICE') or 'proftpd').strip() or 'proftpd'


def pro_systemctl(conf: Dict[str, str], action: str, timeout: int=30) -> Tuple[int, str]:
    return pro_run_cmd(['systemctl', action, pro_service_name(conf)], timeout=timeout)


def pro_systemctl_value(conf: Dict[str, str], action: str) -> str:
    rc, out = pro_systemctl(conf, action, timeout=8)
    text = out.strip()
    return text if rc == 0 and text else 'non'


def pro_detect_binary() -> str:
    return shutil.which('proftpd') or '/usr/sbin/proftpd'


def pro_get_service_status(conf: Dict[str, str], shares: Optional[List[pro_ProftpdShare]]=None) -> Dict[str, str]:
    service = pro_service_name(conf)
    binary = pro_detect_binary()
    conf_path = conf.get('CONF_FILE', pro_DEFAULT_CONFIG.get('CONF_FILE', '/etc/proftpd/conf.d/yoleo.conf'))
    status = {
        'service': service,
        'active': pro_systemctl_value(conf, 'is-active'),
        'enabled': pro_systemctl_value(conf, 'is-enabled'),
        'binary': binary,
        'binary_exists': 'oui' if binary and os.path.exists(binary) else 'non',
        'conf_path': conf_path,
        'conf_exists': 'oui' if conf_path and os.path.exists(conf_path) else 'non',
        'port': conf.get('PORT', '21'),
        'passive_ports': conf.get('PASSIVE_PORTS', '30000 30100'),
        'ftp_root': 'home Linux (~)',
        'shares': '0',
    }
    rc, show = pro_run_cmd(['systemctl', 'show', service, '--property=MainPID,SubState,LoadState,Result', '--no-pager'], timeout=8)
    status['show'] = show if rc == 0 else show
    return status


def pro_parse_passive_ports(raw: str) -> Tuple[str, str]:
    nums = re.findall(r'\d+', str(raw or ''))
    if len(nums) >= 2:
        return (nums[0], nums[1])
    if len(nums) == 1:
        return (nums[0], nums[0])
    return ('30000', '30100')


def pro_validate_host_config(conf: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    service = pro_service_name(conf)
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', service):
        errors.append(f'Nom de service systemd invalide : {service}')
    port = str(conf.get('PORT', '21')).strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        errors.append(f'Port FTP invalide : {port}')
    p1, p2 = pro_parse_passive_ports(conf.get('PASSIVE_PORTS', ''))
    if int(p1) > int(p2):
        errors.append('Plage passive inversée.')
    conf_path = pro_normalize_path(conf.get('CONF_FILE', ''))
    if not conf_path.startswith('/'):
        errors.append(f'Fichier conf ProFTPD invalide : {conf_path}')
    return errors


def pro_share_line_to_obj(raw: str, index: int) -> pro_ProftpdShare:
    """
    Format host actuel : chemin|mode.
    Ancien format Docker conservé en lecture : nom|chemin|mode.
    Le nom n'est plus exposé dans l'UI ; il est seulement dérivé du chemin pour compatibilité interne.
    """
    parts = [p.strip() for p in str(raw or '').split('|')]
    if len(parts) >= 3 and not parts[0].startswith('/'):
        host_path = pro_normalize_path(parts[1])
        mode = parts[2].strip().lower()
    else:
        host_path = pro_normalize_path(parts[0] if parts else '')
        mode = parts[1].strip().lower() if len(parts) >= 2 else 'rw'
    if mode not in {'rw', 'ro'}:
        mode = 'rw'
    name = os.path.basename(host_path.rstrip('/')) or f'dossier{index}'
    return pro_ProftpdShare(index=index, name=name, host_path=host_path, container_path='', mode=mode)

def pro_share_obj_to_line(share: pro_ProftpdShare) -> str:
    safe_path = pro_normalize_path(share.host_path).replace('|', '')
    mode = 'ro' if share.mode == 'ro' else 'rw'
    return f'{safe_path}|{mode}'

def pro_read_host_shares() -> List[pro_ProftpdShare]:
    _svc_ensure_file()
    _values, multi = _svc_read_values()
    shares: List[pro_ProftpdShare] = []
    for idx, raw in enumerate(multi.get('PROFTPD_SHARE', []), 1):
        item = pro_share_line_to_obj(raw, idx)
        if item.name or item.host_path:
            shares.append(item)
    return shares


def pro_write_module_config(conf: Dict[str, str], shares: List[pro_ProftpdShare]) -> None:
    values, multi = _svc_read_values()
    if not values and not multi:
        values = _svc_default_values()
        multi = _svc_default_multi()
    for key in pro_CONFIG_ORDER:
        values['PROFTPD_' + key] = str(conf.get(key, pro_DEFAULT_CONFIG.get(key, '')))
    # ProFTPD ne déclare plus de dossiers : l'accès vient des utilisateurs et droits Linux.
    multi['PROFTPD_SHARE'] = []
    _svc_write_all(values, multi)


def pro_collect_host_shares_from_form() -> List[pro_ProftpdShare]:
    host_paths = request.form.getlist('share_host_path[]')
    modes = request.form.getlist('share_mode[]')
    count = max(len(host_paths), len(modes))
    shares: List[pro_ProftpdShare] = []
    for i in range(count):
        host_path = pro_normalize_path(host_paths[i]) if i < len(host_paths) else ''
        mode = modes[i].strip().lower() if i < len(modes) else 'rw'
        mode = 'ro' if mode == 'ro' else 'rw'
        if not host_path:
            continue
        name = os.path.basename(host_path.rstrip('/')) or f'dossier{i + 1}'
        shares.append(pro_ProftpdShare(i + 1, name, host_path, '', mode))
    return shares

def pro_validate_host_shares(shares: List[pro_ProftpdShare], roots: List[str]) -> List[str]:
    errors: List[str] = []
    seen_paths = set()
    for share in shares:
        path_key = pro_normalize_path(share.host_path)
        if not path_key.startswith('/'):
            errors.append(f'Chemin hôte invalide : {share.host_path}')
        elif not pro_is_under_allowed_root(path_key, roots):
            errors.append(f'Chemin hôte refusé : {path_key} hors BROWSE_ROOTS.')
        if path_key in seen_paths:
            errors.append(f'Dossier FTP en double : {path_key}')
        seen_paths.add(path_key)
        if share.mode not in {'rw', 'ro'}:
            errors.append(f'Mode invalide pour {path_key} : {share.mode}')
    return errors

def pro_generate_host_config_text(conf: Dict[str, str], shares: List[pro_ProftpdShare]) -> str:
    port = str(conf.get('PORT', '21')).strip() or '21'
    p1, p2 = pro_parse_passive_ports(conf.get('PASSIVE_PORTS', '30000 30100'))
    lines = [
        '# Fichier généré par Yoleo - ProFTPD',
        '# Les utilisateurs et permissions viennent du système Linux.',
        '# DefaultRoot ~ enferme chaque utilisateur FTP dans son home Linux.',
        'ServerName "Yoleo FTP"',
        'UseIPv6 off',
        'RequireValidShell off',
        'Umask 022 022',
        f'Port {port}',
        f'PassivePorts {p1} {p2}',
        'DefaultRoot ~',
        '',
        '<Directory ~>',
        '  AllowOverwrite on',
        '  <Limit ALL>',
        '    AllowAll',
        '  </Limit>',
        '</Directory>',
        '',
    ]
    return '\n'.join(lines).rstrip() + '\n'

def pro_apply_host_service(conf: Dict[str, str], shares: List[pro_ProftpdShare]) -> Tuple[int, str]:
    errors = pro_validate_host_config(conf)
    if errors:
        return (1, '\n'.join(errors))
    conf_path = pro_normalize_path(conf.get('CONF_FILE', pro_DEFAULT_CONFIG.get('CONF_FILE', '/etc/proftpd/conf.d/yoleo.conf')))
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)
    backup_msg = ''
    if os.path.exists(conf_path):
        backup_dir = os.path.abspath(os.path.join(NAS_ROOT_DIR, conf.get('BACKUP_DIR', '../backups/proftpd')))
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f'yoleo-proftpd-{time.strftime("%Y%m%d-%H%M%S")}.conf')
        shutil.copy2(conf_path, backup_path)
        backup_msg = f'Backup : {backup_path}\n'
    with open(conf_path, 'w', encoding='utf-8') as handle:
        handle.write(pro_generate_host_config_text(conf, shares))
    rc, out = pro_run_cmd(['systemctl', 'daemon-reload'], timeout=30)
    msg = backup_msg + f'Conf ProFTPD écrite : {conf_path}\n' + out
    return (rc, msg.strip())


def pro_install_package_if_needed() -> Tuple[int, str]:
    binary = pro_detect_binary()
    if binary and os.path.exists(binary):
        return (0, f'ProFTPD déjà installé : {binary}')
    if not shutil.which('apt-get'):
        return (1, 'apt-get introuvable : installation automatique impossible sur ce système.')
    script = 'export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y proftpd-basic'
    return pro_run_cmd(['bash', '-lc', script], timeout=600)



def _render_proftpd():
    conf = pro_get_config()
    shares = []
    status = pro_get_service_status(conf, shares)
    return render_template(
        'services_proftpd.html',
        conf=conf,
        config_file=pro_CONFIG_FILE,
        error='',
        status=status,
        shares=shares,
        allowed_roots=pro_allowed_roots(conf),
        active_subtab='config',
        service_active='proftpd',
    )

@services_bp.route('/services/proftpd/config', methods=['POST'])
def pro_proftpd_config_save():
    conf = pro_get_config()
    for key in pro_CONFIG_ORDER:
        if key in request.form:
            conf[key] = request.form.get(key, '').strip()
    errors = pro_validate_host_config(conf)
    if errors:
        for err in errors:
            flash('❌ ' + err, 'error')
        return services_redirect('proftpd', subtab='config')
    pro_write_module_config(conf, [])
    flash('✅ Configuration ProFTPD enregistrée.', 'success')
    return services_redirect('proftpd', subtab='config')

@services_bp.route('/services/proftpd/save', methods=['POST'])
def pro_proftpd_save():
    conf = pro_get_config()
    pro_write_module_config(conf, [])
    flash('✅ Ancienne liste de dossiers ProFTPD supprimée. Les accès FTP passent par les utilisateurs et droits Linux.', 'success')
    return services_redirect('proftpd', subtab='config')

@services_bp.route('/services/proftpd/check', methods=['POST'])
def pro_proftpd_check():
    conf = pro_get_config()
    shares = pro_read_host_shares()
    errors = pro_validate_host_config(conf)
    output = '\n'.join(errors) if errors else pro_generate_host_config_text(conf, shares)
    return jsonify({'ok': not errors, 'code': 0 if not errors else 1, 'output': output or 'OK'})


@services_bp.route('/services/proftpd/service/action', methods=['POST'])
def pro_proftpd_service_action():
    conf = pro_get_config()
    shares = []
    action = request.form.get('action', '').strip().lower()
    try:
        if action == 'start':
            rc, out = pro_systemctl(conf, 'start')
        elif action == 'stop':
            rc, out = pro_systemctl(conf, 'stop')
        elif action == 'restart':
            pro_apply_host_service(conf, shares)
            rc, out = pro_systemctl(conf, 'restart')
        elif action == 'enable':
            rc, out = pro_systemctl(conf, 'enable')
        elif action == 'disable':
            rc, out = pro_systemctl(conf, 'disable')
        else:
            rc, out = (1, f'Action inconnue : {action}')
        flash(('✅ ' if rc == 0 else '❌ ') + (out or action), 'success' if rc == 0 else 'error')
    except Exception as exc:
        flash(f'❌ Erreur action service ProFTPD : {exc}', 'error')
    return services_redirect('proftpd', subtab='config')


@services_bp.route('/services/proftpd/system/action', methods=['POST'])
def pro_proftpd_system_action():
    conf = pro_get_config()
    shares = []
    action = request.form.get('action', '').strip().lower()
    try:
        if action == 'install':
            rc1, out1 = pro_install_package_if_needed()
            if rc1 != 0:
                flash('❌ Installation ProFTPD en erreur :\n' + out1, 'error')
                return services_redirect('proftpd', subtab='config')
            rc2, out2 = pro_apply_host_service(conf, shares)
            rc3, out3 = pro_systemctl(conf, 'enable') if rc2 == 0 else (rc2, '')
            rc4, out4 = pro_systemctl(conf, 'restart') if rc2 == 0 else (rc2, '')
            ok = rc1 == rc2 == rc3 == rc4 == 0
            flash(('✅ Installation ProFTPD terminée.\n' if ok else '⚠️ Installation ProFTPD partielle.\n') + '\n'.join([out1, out2, out3, out4]), 'success' if ok else 'error')
        elif action == 'apply':
            rc, out = pro_apply_host_service(conf, shares)
            flash(('✅ Configuration ProFTPD appliquée.\n' if rc == 0 else '❌ Application ProFTPD en erreur.\n') + out, 'success' if rc == 0 else 'error')
        else:
            flash(f'❌ Action inconnue : {action}', 'error')
    except Exception as exc:
        flash(f'❌ Erreur action système ProFTPD : {exc}', 'error')
    return services_redirect('proftpd', subtab='config')

@services_bp.route('/services/proftpd/api/browse', methods=['GET'])
def pro_proftpd_browse():
    conf = pro_get_config()
    roots = pro_allowed_roots(conf)
    requested = pro_normalize_path(request.args.get('path') or roots[0])
    if not pro_is_under_allowed_root(requested, roots):
        requested = roots[0]
    real = os.path.realpath(requested)
    if not os.path.isdir(real):
        return (jsonify({'ok': False, 'path': real, 'error': 'Dossier introuvable ou non accessible.', 'items': []}), 404)
    items = []
    try:
        if real not in roots:
            parent = os.path.dirname(real.rstrip('/')) or '/'
            if pro_is_under_allowed_root(parent, roots):
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
# SFTP MERGED MODULE
# ============================================================
sftp_CONFIG_FILE = nas_conf_file("sftp.conf")
sftp_DEFAULT_CONFIG = {
    # YAML volontairement vide : il est dérivé à l'exécution depuis dockers.conf / YML_FOLDER.
    'YAML': '',
    'SERVICE': '',
    'BROWSE_ROOTS': '/mnt/user,/mnt/ramdisk,/boot',
    'BACKUP_DIR': '../backups/sftp',
    'DOCKER_BIN': 'docker',
    'ENABLE_DOCKER_COMPOSE_CHECK': '1',
}
sftp_USER_RE = re.compile('^[a-z_][a-z0-9_-]{0,31}$')
sftp_RESERVED_CONTAINER_TARGETS = {'/data', '/config', '/app', '/tmp', '/run', '/etc', '/root', '/home'}
sftp_YES_NO_KEYS = {'SSH_PERMIT_ROOT', 'SSH_PUBKEY_AUTH', 'SSH_PASS_AUTH', 'SSH_CHALLENGE_AUTH', 'SSH_EMPTY_PASS', 'SSH_USE_PAM', 'SSH_TCP_FORWARD', 'SSH_X11_FORWARD'}
sftp_DEFAULT_ENV = {'TZ': 'Europe/Paris', 'PUID': '99', 'PGID': '100', 'KEY_VAR': '3072', 'SSH_PUBKEY_AUTH': 'yes', 'SSH_PASS_AUTH': 'no', 'SSH_PERMIT_ROOT': 'no', 'SSH_CHALLENGE_AUTH': 'no', 'SSH_EMPTY_PASS': 'no', 'SSH_USE_PAM': 'yes', 'SSH_TCP_FORWARD': 'yes', 'SSH_X11_FORWARD': 'yes'}

