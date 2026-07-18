import io
import os
import sys
import tempfile
import types
import unittest
import zipfile

from flask import Flask

try:
    from yoleo_api import create_yoleo_api_blueprint
except ModuleNotFoundError:
    # Permet aussi d'ajouter ce fichier à la suite de yoleo_api.py et de
    # l'exécuter en mémoire sur le Python du serveur, sans aucun déploiement.
    pass


class YoleoApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_root = os.path.join(self.temp_dir.name, "nas")
        os.mkdir(self.file_root)
        self.previous_file_roots = os.environ.get("YOLEO_API_FILE_ROOTS")
        os.environ["YOLEO_API_FILE_ROOTS"] = self.file_root
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(
            create_yoleo_api_blueprint(
                authenticate_user=lambda username, password: username == "root" and password == "secret",
                allowed_user="root",
                token_db_path=self.temp_dir.name + "/api_tokens.sqlite3",
            )
        )
        self.client = self.app.test_client()

    def tearDown(self):
        if self.previous_file_roots is None:
            os.environ.pop("YOLEO_API_FILE_ROOTS", None)
        else:
            os.environ["YOLEO_API_FILE_ROOTS"] = self.previous_file_roots
        self.temp_dir.cleanup()

    def login(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "root",
                "password": "secret",
                "device_name": "Test Windows",
                "platform": "windows",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["authentication"]["access_token"]

    @staticmethod
    def authorization(token):
        return {"Authorization": "Bearer " + token}

    def test_health_is_public_and_json(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["api_version"], "1")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_protected_route_rejects_missing_token(self):
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "authentication_required")

    def test_login_requires_json_and_valid_credentials(self):
        response = self.client.post("/api/v1/auth/login", data="username=root")
        self.assertEqual(response.status_code, 415)

        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": "root", "password": "incorrect"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_credentials")

    def test_token_lifecycle(self):
        token = self.login()
        headers = self.authorization(token)

        response = self.client.get("/api/v1/me", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["identity"]["username"], "root")
        self.assertEqual(response.get_json()["identity"]["platform"], "windows")

        response = self.client.post("/api/v1/auth/logout", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["revoked"])

        response = self.client.get("/api/v1/me", headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_overview_reuses_existing_collector(self):
        token = self.login()
        previous = sys.modules.get("system")
        sys.modules["system"] = types.SimpleNamespace(
            collect_overview=lambda: {"cpu": {"percent": 12}, "docker": {"running": 3}}
        )
        try:
            response = self.client.get("/api/v1/overview", headers=self.authorization(token))
        finally:
            if previous is None:
                sys.modules.pop("system", None)
            else:
                sys.modules["system"] = previous

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["overview"]["docker"]["running"], 3)

    def test_docker_action_is_json_and_whitelisted(self):
        token = self.login()
        headers = self.authorization(token)

        response = self.client.post(
            "/api/v1/docker/actions",
            headers=headers,
            json={"action": "shell", "command": "anything"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "action_not_allowed")

        closed = []

        class FakeClient:
            def close(self):
                closed.append(True)

        previous = sys.modules.get("dockers")
        sys.modules["dockers"] = types.SimpleNamespace(
            get_docker_client=lambda: FakeClient(),
            do_action=lambda client, container_id, action: (
                {"status": "success", "message": action + ":" + container_id},
                200,
            ),
            do_docker_service_action=lambda action: ({"status": "success", "message": action}, 200),
            clean_docker_error=lambda error: str(error),
        )
        try:
            response = self.client.post(
                "/api/v1/docker/actions",
                headers=headers,
                json={"action": "restart", "container_id": "media"},
            )
        finally:
            if previous is None:
                sys.modules.pop("dockers", None)
            else:
                sys.modules["dockers"] = previous

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["message"], "restart:media")
        self.assertEqual(closed, [True])

    def test_vm_action_is_json_whitelisted_and_never_creates_a_vm(self):
        token = self.login()
        headers = self.authorization(token)

        response = self.client.post(
            "/api/v1/vm/actions",
            headers=headers,
            json={"action": "define", "name": "Windows"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "action_not_allowed")

        previous = sys.modules.get("vm")
        calls = []
        sys.modules["vm"] = types.SimpleNamespace(
            get_config=lambda: {"LIBVIRT_URI": "qemu:///system"},
            do_vm_action=lambda conf, name, action: (
                calls.append((name, action)) or {"ok": True, "message": "Redémarrage demandé."},
                200,
            ),
        )
        try:
            response = self.client.post(
                "/api/v1/vm/actions",
                headers=headers,
                json={"action": "reboot", "name": "Windows"},
            )
        finally:
            if previous is None:
                sys.modules.pop("vm", None)
            else:
                sys.modules["vm"] = previous

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(calls, [("Windows", "reboot")])

    def test_task_action_reuses_only_start_and_stop(self):
        token = self.login()
        headers = self.authorization(token)
        response = self.client.post(
            "/api/v1/task/actions",
            headers=headers,
            json={"action": "delete", "task_id": 7},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "action_not_allowed")

        previous = sys.modules.get("task")
        calls = []
        sys.modules["task"] = types.SimpleNamespace(
            get_task=lambda task_id: {"id": task_id, "title": "Sauvegarde"},
            run_task_background=lambda task_id, source: (
                calls.append((task_id, "start", source)) or True,
                "Tâche lancée.",
            ),
            force_stop_task=lambda task_id, source: (
                calls.append((task_id, "stop", source)) or True,
                "Arrêt demandé.",
            ),
        )
        try:
            response = self.client.post(
                "/api/v1/task/actions",
                headers=headers,
                json={"action": "start", "task_id": 7},
            )
        finally:
            if previous is None:
                sys.modules.pop("task", None)
            else:
                sys.modules["task"] = previous

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(response.get_json()["task_action"]["accepted"])
        self.assertEqual(response.get_json()["message"], "Tâche lancée.")
        self.assertEqual(response.get_json()["task_action"]["message"], "Tâche lancée.")
        self.assertEqual(calls, [(7, "start", "API Android")])

    def test_backup_action_reuses_only_existing_scripts_and_start_stop(self):
        token = self.login()
        headers = self.authorization(token)
        response = self.client.post(
            "/api/v1/backup/actions",
            headers=headers,
            json={"action": "delete", "filename": "photos.py"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "action_not_allowed")

        previous = sys.modules.get("backup")
        calls = []
        sys.modules["backup"] = types.SimpleNamespace(
            list_scripts=lambda: [{"name": "photos.py", "title": "Photos"}],
            run_script=lambda filename: (
                calls.append((filename, "start")) or {
                    "ok": True,
                    "message": "Backup lancé",
                    "status": {"running": True, "result": "En cours"},
                }
            ),
            stop_script=lambda filename: (
                calls.append((filename, "stop")) or {
                    "ok": True,
                    "message": "Backup arrêté",
                    "status": {"running": False, "result": "Arrêté"},
                }
            ),
        )
        try:
            missing = self.client.post(
                "/api/v1/backup/actions",
                headers=headers,
                json={"action": "start", "filename": "inconnu.py"},
            )
            response = self.client.post(
                "/api/v1/backup/actions",
                headers=headers,
                json={"action": "start", "filename": "photos.py"},
            )
        finally:
            if previous is None:
                sys.modules.pop("backup", None)
            else:
                sys.modules["backup"] = previous

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"]["code"], "backup_not_found")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["backup_action"]["accepted"])
        self.assertTrue(response.get_json()["backup_action"]["status"]["running"])
        self.assertEqual(calls, [("photos.py", "start")])

    def test_file_api_lists_and_rejects_paths_outside_nas_roots(self):
        token = self.login()
        headers = self.authorization(token)
        os.mkdir(os.path.join(self.file_root, "Photos"))
        with open(os.path.join(self.file_root, "note.txt"), "wb") as stream:
            stream.write(b"Yoleo")

        response = self.client.post(
            "/api/v1/files/list",
            headers=headers,
            json={"path": self.file_root},
        )
        self.assertEqual(response.status_code, 200)
        files = response.get_json()["files"]
        self.assertEqual(files["root"], self.file_root)
        self.assertEqual([item["name"] for item in files["items"]], ["Photos", "note.txt"])

        forbidden = self.client.post(
            "/api/v1/files/list",
            headers=headers,
            json={"path": os.path.dirname(self.file_root)},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.get_json()["error"]["code"], "path_forbidden")

    def test_file_api_creates_renames_copies_moves_and_deletes(self):
        token = self.login()
        headers = self.authorization(token)
        source = os.path.join(self.file_root, "source.txt")
        with open(source, "wb") as stream:
            stream.write(b"contenu")

        mkdir = self.client.post(
            "/api/v1/files/actions",
            headers=headers,
            json={"action": "mkdir", "directory": self.file_root, "name": "Destination"},
        )
        self.assertEqual(mkdir.status_code, 200)
        destination = os.path.join(self.file_root, "Destination")

        renamed = self.client.post(
            "/api/v1/files/actions",
            headers=headers,
            json={"action": "rename", "source": source, "name": "renomme.txt"},
        )
        self.assertEqual(renamed.status_code, 200)
        renamed_path = os.path.join(self.file_root, "renomme.txt")

        copied = self.client.post(
            "/api/v1/files/actions",
            headers=headers,
            json={"action": "copy", "source": renamed_path, "destination": destination},
        )
        self.assertEqual(copied.status_code, 200)
        copied_path = os.path.join(destination, "renomme.txt")
        self.assertTrue(os.path.isfile(copied_path))

        deleted = self.client.post(
            "/api/v1/files/actions",
            headers=headers,
            json={"action": "delete", "source": copied_path},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(os.path.exists(copied_path))

    def test_file_api_uploads_and_downloads_without_overwriting(self):
        token = self.login()
        headers = self.authorization(token)
        response = self.client.post(
            "/api/v1/files/upload",
            headers=headers,
            data={
                "path": self.file_root,
                "file": (io.BytesIO(b"photo-mobile"), "photo.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)

        duplicate = self.client.post(
            "/api/v1/files/upload",
            headers=headers,
            data={
                "path": self.file_root,
                "file": (io.BytesIO(b"autre"), "photo.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 409)

        corrupted = self.client.post(
            "/api/v1/files/upload",
            headers=headers,
            data={
                "path": self.file_root,
                "overwrite": "true",
                "sha256": "0" * 64,
                "file": (io.BytesIO(b"contenu-corrompu"), "photo.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(corrupted.status_code, 422)
        self.assertEqual(corrupted.get_json()["error"]["code"], "sha256_mismatch")

        downloaded = self.client.get(
            "/api/v1/files/download",
            headers=headers,
            query_string={"path": os.path.join(self.file_root, "photo.jpg")},
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"photo-mobile")
        downloaded.close()

    def test_file_api_downloads_directory_as_zip(self):
        token = self.login()
        headers = self.authorization(token)
        folder = os.path.join(self.file_root, "Applications")
        nested = os.path.join(folder, "Android")
        os.makedirs(nested)
        with open(os.path.join(nested, "Yoleo.apk"), "wb") as stream:
            stream.write(b"apk-yoleo")

        response = self.client.get(
            "/api/v1/files/download",
            headers=headers,
            query_string={"path": folder, "archive": "zip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.data), "r") as archive:
            self.assertEqual(
                archive.read("Applications/Android/Yoleo.apk"),
                b"apk-yoleo",
            )
        response.close()

    def test_file_catalog_uses_relative_sha256_entries(self):
        token = self.login()
        headers = self.authorization(token)
        folder = os.path.join(self.file_root, "Photos")
        os.mkdir(folder)
        with open(os.path.join(folder, "image.jpg"), "wb") as stream:
            stream.write(b"photo-yoleo")

        response = self.client.post(
            "/api/v1/files/catalog",
            headers=headers,
            json={"path": self.file_root},
        )
        self.assertEqual(response.status_code, 200)
        catalog = response.get_json()["catalog"]
        self.assertEqual(catalog["algorithm"], "SHA-256")
        self.assertFalse(catalog["truncated"])
        by_path = {item["relative_path"]: item for item in catalog["entries"]}
        self.assertTrue(by_path["Photos"]["is_dir"])
        self.assertEqual(
            by_path["Photos/image.jpg"]["sha256"],
            "d838da24e23c2ce6e47c88b814df8cfc92d2896787084682493e3b83ed0cf4ff",
        )

    def test_monitoring_snapshot_combines_sections_without_exposing_commands(self):
        token = self.login()
        headers = self.authorization(token)
        closed = []

        class FakeClient:
            def close(self):
                closed.append(True)

        fake_modules = {
            "system": types.SimpleNamespace(
                collect_overview=lambda: {
                    "cpu": {"percent": 91.5},
                    "ram": {"percent": 72.0},
                    "disk": {"mount": "/mnt/user", "percent": 66, "used": "6 Tio", "total": "10 Tio"},
                    "mounts": {
                        "state": "ok",
                        "label": "OK",
                        "usage_rows": [
                            {
                                "path": "/mnt/cache",
                                "percent": 81,
                                "used": "810 Gio",
                                "free": "190 Gio",
                                "total": "1 Tio",
                                "status": "ok",
                                "status_label": "OK",
                                "ok": True,
                            }
                        ],
                    },
                    "build": {"available": True, "to_build": 2, "to_push": 1, "total": 8},
                    "uptime": "4 jours",
                    "host": {
                        "hostname": "system",
                        "os": "Debian",
                        "kernel": "6.12",
                        "cpu_model": "Intel Test",
                        "local_ip": "192.168.1.2",
                        "boot_time": "10/07/2026 10:00:00",
                    },
                    "network": {
                        "iface": "br0",
                        "ip": "192.168.1.2",
                        "gateway": "192.168.1.1",
                        "state": "up",
                        "speed": "2.5 Gbit/s",
                    },
                    "services": {"total": 100, "active": 40, "running": 38, "failed": 1, "enabled": 50},
                    "fans": {
                        "rows": [{"id": "nct:1", "label": "CPU FAN", "rpm": 800,
                                  "rpm_label": "800 RPM", "status": "ok"}],
                    },
                },
                collect_mobile_hardware_stats=lambda: {
                    "temperatures": [{"id": "cpu:1", "chip": "cpu", "label": "CPU",
                                      "current": 44.5, "high": 80, "critical": 100}],
                    "gpus": [{"type": "intel", "source": "local", "label": "Intel GPU",
                              "name": "Intel UHD", "load": "8", "mem": "0", "temp": "45",
                              "power": "6.2", "fan": "-"}],
                },
                disk_top_collect_candidates=lambda: {
                    "rows": [
                        {
                            "path": "/mnt/cache",
                            "is_mount": True,
                            "exists": True,
                            "source": "/dev/nvme0n1p1",
                            "fstype": "xfs",
                            "status": "ok",
                            "status_label": "OK",
                            "usage_selected": True,
                        },
                        {
                            "path": "/mnt/media0",
                            "is_mount": False,
                            "exists": True,
                            "status": "folder",
                            "status_label": "Dossier",
                        },
                        {
                            "path": "/mnt/media1",
                            "is_mount": True,
                            "exists": True,
                            "source": "/dev/sdb1",
                            "fstype": "ext4",
                            "status": "ok",
                            "status_label": "OK",
                        },
                    ]
                },
                disk_top_findmnt_map=lambda: {
                    "/mnt/cache": {"source": "/dev/nvme0n1p1", "fstype": "xfs"},
                    "/mnt/media1": {"source": "/dev/sdb1", "fstype": "ext4"},
                },
                disk_top_usage_row=lambda path, mounted: {
                    "path": path,
                    "percent": 55,
                    "used": "550 Gio",
                    "free": "450 Gio",
                    "total": "1 Tio",
                    "status": "ok",
                    "status_label": "OK",
                    "ok": True,
                },
            ),
            "dockers": types.SimpleNamespace(
                get_docker_service_status=lambda: {"state": "active", "label": "Actif", "active": True},
                get_docker_client=lambda: FakeClient(),
                list_stacks=lambda client: {
                    "Media": [
                        {
                            "id": "abc",
                            "name": "emby",
                            "state": "running",
                            "icon": "https://example.test/emby.png",
                            "secret": "ignored",
                        },
                        {"id": "def", "name": "sonarr", "state": "exited"},
                    ]
                },
                get_docker_stats=lambda stacks: {"total": 2, "running": 1, "stopped": 1},
                clean_docker_error=lambda error: str(error),
            ),
            "partage": types.SimpleNamespace(
                SAMBA_SERVICES=("smbd.service", "nmbd.service"),
                DISTRO_WSDD_SERVICES=("wsdd2.service",),
                service_state=lambda name: {
                    "name": name,
                    "exists": name != "wsdd2.service",
                    "active": "active",
                    "enabled": "enabled",
                    "ok": True,
                },
            ),
            "task": types.SimpleNamespace(
                get_all_tasks=lambda: [
                    {
                        "id": 7,
                        "title": "Sauvegarde",
                        "enabled": 1,
                        "commands": ["mot-de-passe-secret"],
                        "status": {
                            "running": 0,
                            "status": "Erreur",
                            "result": "Erreur",
                            "last_run": "2026-07-14 10:00:00",
                            "last_end": "2026-07-14 10:01:00",
                            "last_message": "code retour 1",
                            "updated_at": "2026-07-14 10:01:00",
                        },
                    }
                ]
            ),
            "backup": types.SimpleNamespace(
                list_scripts=lambda: [
                    {
                        "name": "photos.py",
                        "title": "Photos",
                        "mode": "backup",
                        "source": "/mnt/photos/",
                        "target": "/mnt/backup/photos/",
                        "status": {
                            "running": True,
                            "result": "En cours",
                            "message": "Copie",
                            "progress": {"percent": 42},
                            "progress_text": "42 %",
                        },
                    }
                ]
            ),
            "vm": types.SimpleNamespace(
                get_config=lambda: {"LIBVIRT_URI": "qemu:///system"},
                list_vm_names=lambda conf: (["Windows", "Debian"], None),
                get_vm_state=lambda conf, name: "running" if name == "Windows" else "shut off",
                state_class=lambda state: "running" if state == "running" else "stopped",
            ),
        }
        previous = {name: sys.modules.get(name) for name in fake_modules}
        sys.modules.update(fake_modules)
        try:
            response = self.client.get("/api/v1/monitoring/snapshot", headers=headers)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["monitoring"]
        self.assertEqual(payload["system"]["cpu_percent"], 91.5)
        self.assertEqual(payload["system"]["host"]["local_ip"], "192.168.1.2")
        self.assertEqual(payload["system"]["services"]["active"], 40)
        self.assertEqual(payload["system"]["temperatures"][0]["current"], 44.5)
        self.assertEqual(payload["system"]["fans"]["rows"][0]["rpm"], 800)
        self.assertEqual(payload["system"]["gpus"][0]["name"], "Intel UHD")
        self.assertEqual(payload["storage"]["volumes"][0]["path"], "/mnt/cache")
        self.assertEqual(len(payload["storage"]["mounts"]), 3)
        self.assertTrue(payload["storage"]["mounts"][0]["is_mount"])
        self.assertEqual(payload["storage"]["mounts"][1]["status"], "folder")
        self.assertEqual(payload["storage"]["mounts"][2]["percent"], 55)
        self.assertEqual(payload["docker"]["stats"]["stopped"], 1)
        self.assertEqual(payload["docker"]["containers"][0]["icon"], "https://example.test/emby.png")
        self.assertEqual(payload["docker"]["containers"][1]["state"], "exited")
        self.assertEqual(payload["vms"]["summary"]["running"], 1)
        self.assertEqual(payload["vms"]["summary"]["stopped"], 1)
        self.assertTrue(payload["samba"]["ok"])
        self.assertEqual(payload["tasks"][0]["result"], "Erreur")
        self.assertNotIn("commands", payload["tasks"][0])
        self.assertTrue(payload["backup"]["scripts"][0]["running"])
        self.assertEqual(payload["backup"]["scripts"][0]["progress_percent"], 42)
        self.assertEqual(payload["build"]["to_build"], 2)
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
