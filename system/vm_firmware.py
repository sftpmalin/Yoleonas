"""Known UEFI firmware/key pairs and reversible per-VM firmware profiles."""
from __future__ import annotations

import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

PROFILE_NAMESPACE = 'urn:yoleo:vm:firmware:1'
PROFILE_TAG = '{' + PROFILE_NAMESPACE + '}firmware'
ET.register_namespace('yoleo', PROFILE_NAMESPACE)
PROFILES = (
    {'id': 'windows_uefi', 'label': 'Windows UEFI', 'description': 'Sans Secure Boot', 'secure_boot': False},
    {'id': 'windows_secure', 'label': 'Windows UEFI Secure Boot', 'description': 'Avec les clés standard Windows', 'secure_boot': True},
    {'id': 'linux_uefi', 'label': 'Linux UEFI normal', 'description': 'Sans Secure Boot', 'secure_boot': False},
    {'id': 'linux_secure', 'label': 'Linux UEFI Secure Boot', 'description': 'Clés standard compatibles Debian / Ubuntu', 'secure_boot': True},
)


def template_has_keys(template: str) -> bool:
    name = Path(template).name.lower()
    return '.ms.' in name or 'snakeoil' in name


def resolve_profile(profile_id: str, arch: str = 'x86_64', isfile=None) -> dict:
    definition = next((row for row in PROFILES if row['id'] == profile_id), None)
    if definition is None:
        raise ValueError('Profil de firmware inconnu.')
    if arch not in {'', 'x86_64', 'amd64'}:
        raise ValueError('Ces profils UEFI sont prévus pour les VM x86_64.')
    isfile = isfile or os.path.isfile
    pairs = []
    for directory in ['/usr/share/OVMF', '/usr/share/ovmf']:
        for size in ['_4M', '']:
            if definition['secure_boot']:
                pairs.extend([
                    (f'{directory}/OVMF_CODE{size}.ms.fd', f'{directory}/OVMF_VARS{size}.ms.fd'),
                    (f'{directory}/OVMF_CODE{size}.secboot.fd', f'{directory}/OVMF_VARS{size}.ms.fd'),
                ])
            else:
                pairs.append((f'{directory}/OVMF_CODE{size}.fd', f'{directory}/OVMF_VARS{size}.fd'))
    for loader, nvram in pairs:
        if isfile(loader) and isfile(nvram):
            return {**definition, 'loader': loader, 'nvram': nvram, 'available': True}
    raise FileNotFoundError('Le couple de firmware et de clés UEFI requis par ce profil est absent du NAS.')


def profile_choices(arch: str = 'x86_64') -> list[dict]:
    rows = []
    for definition in PROFILES:
        try:
            rows.append(resolve_profile(definition['id'], arch))
        except (ValueError, FileNotFoundError) as exc:
            rows.append({**definition, 'available': False, 'reason': str(exc)})
    return rows


def configured_secure_boot(root: ET.Element) -> bool:
    template = root.find('./os/nvram')
    if template is not None and template.get('template'):
        # The loader's secure=yes means capable; enrolled keys enable it.
        return template_has_keys(template.get('template', ''))
    feature = root.find("./os/firmware/feature[@name='secure-boot']")
    if feature is not None:
        return feature.get('enabled', '').lower() in {'yes', 'on', 'true', '1'}
    loader = root.find('./os/loader')
    return loader is not None and loader.get('secure', '').lower() in {'yes', 'on', 'true', '1'}


def saved_profile(root: ET.Element) -> str:
    node = root.find('./metadata/' + PROFILE_TAG)
    value = node.get('profile', '') if node is not None else ''
    return value if any(row['id'] == value for row in PROFILES) else ''


def clear_profile(root: ET.Element) -> None:
    metadata = root.find('metadata')
    if metadata is not None:
        for node in list(metadata.findall(PROFILE_TAG)):
            metadata.remove(node)


def apply_profile(root: ET.Element, profile: dict) -> dict:
    os_node = root.find('os')
    if os_node is None:
        raise ValueError('Configuration système de la VM absente.')
    old_nvram = os_node.find('nvram')
    old_template = old_nvram.get('template', '') if old_nvram is not None else ''
    old_path = (old_nvram.text or '').strip() if old_nvram is not None else ''
    refresh = not old_path or old_template != profile['nvram'] or not saved_profile(root)
    # A new variable store is initialized by libvirt from the chosen template.
    # The previous store and its keys remain intact for recovery via XML backup.
    if refresh:
        directory = Path(old_path).parent if old_path and os.path.isabs(old_path) else Path('/var/lib/libvirt/qemu/nvram')
        nvram_path = str(directory / ('yoleo-' + uuid.uuid4().hex + '_VARS.fd'))
    else:
        nvram_path = old_path
    os_node.attrib.pop('firmware', None)
    for tag in ['firmware', 'loader', 'nvram']:
        for node in list(os_node.findall(tag)):
            os_node.remove(node)
    loader = ET.SubElement(os_node, 'loader', {'readonly': 'yes', 'type': 'pflash', 'format': 'raw', 'secure': 'yes' if profile['secure_boot'] else 'no'})
    loader.text = profile['loader']
    ET.SubElement(os_node, 'nvram', {'template': profile['nvram'], 'format': 'raw'}).text = nvram_path
    if profile['secure_boot']:
        features = root.find('features')
        if features is None:
            features = ET.SubElement(root, 'features')
        smm = features.find('smm')
        if smm is None:
            smm = ET.SubElement(features, 'smm')
        smm.set('state', 'on')
    clear_profile(root)
    metadata = root.find('metadata')
    if metadata is None:
        metadata = ET.SubElement(root, 'metadata')
    ET.SubElement(metadata, PROFILE_TAG, {'profile': profile['id']})
    return {'nvram_refreshed': refresh, 'previous_nvram': old_path if refresh else '', 'nvram': nvram_path}
