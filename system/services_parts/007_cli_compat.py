if __name__ == "__main__":
    if "--archive-worker" in sys.argv:
        raise SystemExit(archive_worker_cli(sys.argv[1:]))


mini_CONFIG_FILE = SERVICES_CONFIG_FILE
pro_CONFIG_FILE = SERVICES_CONFIG_FILE
sftp_CONFIG_FILE = SERVICES_CONFIG_FILE

mini_read_module_config = _svc_prefixed_mini
mini_write_module_config = _svc_write_mini


def pro_get_config() -> Dict[str, str]:
    # ProFTPD est désormais géré en host : plus de YAML Docker ni d'utilisateurs virtuels ici.
    conf = _svc_prefixed_config("PROFTPD_", pro_DEFAULT_CONFIG, pro_CONFIG_ORDER)
    return conf


def pro_write_kv_file(path: str, data: Dict[str, str]) -> None:
    # Conservation des dossiers déclarés pendant la sauvegarde de la configuration.
    pro_write_module_config(data, pro_read_host_shares())


def sftp_get_config() -> Dict[str, str]:
    # Le YAML SFTP n'est plus imposé par nom. On le détecte depuis le dossier YAML du module Docker.
    order = ["YAML", "SERVICE", "BROWSE_ROOTS", "BACKUP_DIR", "DOCKER_BIN", "ENABLE_DOCKER_COMPOSE_CHECK"]
    conf = _svc_prefixed_config("SFTP_", sftp_DEFAULT_CONFIG, order)
    conf, changed, _message = sftp_complete_config_from_autodetect(conf)
    yaml_path = str(conf.get("YAML") or "").strip()
    if yaml_path and os.path.exists(yaml_path):
        conf["_DOCKER_YAML_ERROR"] = ""
        conf["_YAML_SOURCE"] = "détection automatique depuis le dossier YAML Docker"
    else:
        conf["_DOCKER_YAML_ERROR"] = sftp_yaml_missing_message(conf, yaml_path)
        conf["_YAML_SOURCE"] = "détection automatique non aboutie"
    if changed:
        _svc_update_prefixed("SFTP_", conf, order)
    return conf


def sftp_write_kv_file(path: str, data: Dict[str, str]) -> None:
    # Conservation du YAML détecté comme cache, mais l'UI ne demande plus de le saisir à la main.
    _svc_update_prefixed("SFTP_", data, ["YAML", "SERVICE", "BROWSE_ROOTS", "BACKUP_DIR", "DOCKER_BIN", "ENABLE_DOCKER_COMPOSE_CHECK"])
