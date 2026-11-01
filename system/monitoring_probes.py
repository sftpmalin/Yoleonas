"""Read-only monitoring: a slow mount or daemon never occupies web threads."""

import copy
from dataclasses import dataclass
import os
import threading
import time

import psutil


@dataclass(frozen=True)
class ProbeResult:
    value: object = None
    pending: bool = False
    error: str = ""


class ProbeCache:
    """Keep the last result and at most one in-flight read for each key.

    The total number of background reads is bounded too. A kernel I/O wait
    cannot be cancelled by Python, so timed-out callers must not start new
    threads for the same read on every dashboard refresh.
    """

    def __init__(self, max_pending=4):
        self._lock = threading.Lock()
        self._entries = {}
        self._pending = 0
        self._max_pending = max_pending

    def read(self, key, collector, *, ttl=5.0, wait=0.02):
        start = False
        with self._lock:
            entry = self._entries.setdefault(key, {
                'value': None, 'error': '', 'updated': 0.0,
                'pending': False, 'done': threading.Event(),
            })
            if (not entry['pending'] and self._pending < self._max_pending
                    and (entry['updated'] == 0.0 or time.monotonic() - entry['updated'] >= ttl)):
                entry['pending'] = True
                entry['done'].clear()
                self._pending += 1
                start = True
            done = entry['done']
        if start:
            try:
                threading.Thread(target=self._refresh, args=(entry, collector),
                                 name='yoleo-monitoring-probe', daemon=True).start()
            except Exception:
                with self._lock:
                    entry['pending'] = False
                    self._pending -= 1
                    done.set()
                raise
        if wait > 0:
            done.wait(wait)
        with self._lock:
            return ProbeResult(copy.deepcopy(entry['value']), entry['pending'], entry['error'])

    def _refresh(self, entry, collector):
        try:
            value = collector()
            error = ''
        except Exception as exc:
            value = None
            error = str(exc)[:160] or type(exc).__name__
        with self._lock:
            if not error:
                entry['value'] = value
            entry['error'] = error
            entry['updated'] = time.monotonic()
            entry['pending'] = False
            self._pending -= 1
            entry['done'].set()


_LOCAL_PROBES = ProbeCache(max_pending=4)
_REMOTE_PROBES = ProbeCache(max_pending=4)
_DIRECTORY_PROBES = ProbeCache(max_pending=1)
_DOCKER_PROBES = ProbeCache(max_pending=1)


def _path_pool(path, mounted):
    owner = max((target for target in mounted
                 if path == target or path.startswith(target.rstrip('/') + '/')),
                key=len, default='')
    info = mounted.get(owner) or {}
    filesystem = str(info.get('fstype') or '').lower()
    remote = filesystem in {'nfs', 'nfs4', 'cifs', 'smb3', 'fuse.sshfs', '9p'}
    return _REMOTE_PROBES if remote else _LOCAL_PROBES


def mount_path_state(path, mounted):
    # The kernel's mount table already proves that a mount exists. stat() on
    # that path would contact an unavailable NFS/SMB server unnecessarily.
    if path in mounted:
        return {'exists': True, 'is_mount': True, 'has_local_entries': False}

    def inspect_path():
        exists = os.path.exists(path)
        is_mount = os.path.ismount(path) if exists else False
        has_entries = False
        if exists and not is_mount and os.path.isdir(path):
            with os.scandir(path) as entries:
                has_entries = next(entries, None) is not None
        return {'exists': exists, 'is_mount': is_mount, 'has_local_entries': has_entries}

    result = _path_pool(path, mounted).read(('metadata', path), inspect_path)
    return result.value or {'exists': None, 'is_mount': False, 'has_local_entries': False}


def filesystem_usage(path, mounted):
    return _path_pool(path, mounted).read(('usage', path), lambda: psutil.disk_usage(path))


def standard_directory_paths(mounted):
    def scan():
        paths = set()
        for base in ('/mnt', '/media', '/srv'):
            if _path_pool(base, mounted) is _REMOTE_PROBES:
                continue
            try:
                with os.scandir(base) as entries:
                    for entry in entries:
                        if entry.path in mounted or entry.is_dir(follow_symlinks=False):
                            paths.add(entry.path)
            except OSError:
                pass
        for path in ('/mnt/cache', '/mnt/user', '/mnt/disk1', '/mnt/disk2', '/mnt/disk3', '/mnt/disk4'):
            if path in mounted or os.path.exists(path):
                paths.add(path)
        return sorted(paths)

    return _DIRECTORY_PROBES.read('standard-paths', scan).value or []


def docker_counts(docker_module):
    if docker_module is None:
        return {'total': 0, 'running': 0, 'available': False}

    def collect():
        client = docker_module.from_env(timeout=2)
        try:
            # The summary endpoint already includes State. containers.list()
            # would inspect every container again on every monitoring poll.
            containers = client.api.containers(all=True)
            return {'total': len(containers),
                    'running': sum(item.get('State') == 'running' for item in containers),
                    'available': True}
        finally:
            client.close()

    result = _DOCKER_PROBES.read('counts', collect, ttl=3)
    return result.value or {'total': 0, 'running': 0, 'available': False}
