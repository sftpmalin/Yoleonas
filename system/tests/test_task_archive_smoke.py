#!/usr/bin/env python3
"""Smoke test autonome de l'archivage des tâches."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from flask import Flask
from jinja2 import Environment


SYSTEM_DIR = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="yoleo-task-archive-") as tmp_dir:
        conf_dir = Path(tmp_dir) / "conf"
        os.environ["TASK_CONF_DIR"] = str(conf_dir)
        os.environ["TASK_LOG_DIR"] = str(Path(tmp_dir) / "logs")
        os.environ["YOLEO_TASK_STARTUP_CRON_SYNC"] = "0"
        sys.path.insert(0, str(SYSTEM_DIR))

        import task

        # Le test ne doit jamais remplacer le crontab de la machine qui l'exécute.
        task.reload_cron = lambda cron_file: (True, f"cron test: {cron_file}")

        with task.connect_db() as db:
            cur = db.execute(
                """
                INSERT INTO tasks(title, schedule_type, time_hour, time_minute, created_at, updated_at)
                VALUES(?, 'daily', 2, 30, ?, ?)
                """,
                ("Tâche test archive", task.now_str(), task.now_str()),
            )
            task_id = cur.lastrowid
            db.execute(
                "INSERT INTO task_commands(task_id, position, command) VALUES(?, 1, 'echo archive-test')",
                (task_id,),
            )
            db.commit()

        app = Flask(__name__)
        app.register_blueprint(task.task_bp)
        client = app.test_client()

        archived = client.post(f"/system/task/delete/{task_id}?ajax=1")
        assert archived.status_code == 200, archived.get_data(as_text=True)
        assert archived.get_json()["archived"] is True
        assert task.get_task(task_id) is None
        assert task.get_task(task_id, include_archived=True)["archived"] == 1
        assert any(row["id"] == task_id for row in task.get_archived_tasks())

        restored = client.post(f"/system/task/restore/{task_id}?ajax=1")
        assert restored.status_code == 200, restored.get_data(as_text=True)
        assert restored.get_json()["restored"] is True
        assert task.get_task(task_id) is not None

        client.post(f"/system/task/delete/{task_id}?ajax=1")
        deleted = client.post(f"/system/task/delete-forever/{task_id}?ajax=1")
        assert deleted.status_code == 200, deleted.get_data(as_text=True)
        assert deleted.get_json()["deleted_forever"] is True
        assert task.get_task(task_id, include_archived=True) is None

        template_text = (SYSTEM_DIR / "templates" / "system_task.html").read_text(encoding="utf-8")
        Environment().parse(template_text)
        print("task-archive-smoke-ok")


if __name__ == "__main__":
    main()
