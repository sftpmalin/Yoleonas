# vm_parts

Chunks loaded by `../vm.py` in the same Python global namespace.

- `001_part_config_core.py` — PART 001 : imports, blueprint, configuration, helpers communs.
- `002_part_inventory_actions.py` — PART 002 : inventaire VM, parsing XML, actions simples, export XML.
- `003_part_console_novnc.py` — PART 003 : console noVNC, websockify, proxy WebSocket.
- `004_part_device_editing.py` — PART 004 : édition VM, disques, réseaux, périphériques PCI/USB.
- `005_part_host_inventory_ttyd.py` — PART 005 : inventaire hôte, pools, réseaux, ISO, firmware, ttyd série.
- `006_part_storage_network_actions.py` — PART 006 : actions pools de stockage et réseaux libvirt.
- `007_part_create_vm.py` — PART 007 : création VM via virt-install.
- `008_part_routes_pages_api.py` — PART 008 : routes API et rendu des pages.
