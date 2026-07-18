# Backup module chunks

`backup.py` is the stable import entry point used by `app.conf`.
The implementation is split into numbered chunks and loaded in order with
`_module_chunks.load_module_chunks`, like the VM module.

- `001_config_form.py`: configuration, metadata, form normalization.
- `002_generated_script.py`: generated standalone script template.
- `003_status_progress.py`: status files, locks, progress parsing.
- `004_network_legacy.py`: old network/NFS helpers kept for compatibility.
- `005_routes_pages.py`: page routes and save/delete/settings routes.
- `006_run_stop_api.py`: detached launch, stop, logs/status APIs.