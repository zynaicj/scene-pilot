"""SQLite lifecycle, revision checks and crash recovery without external services."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scene_pilot.api.models import PlanningResult, TaskError
from scene_pilot.api.store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.store.open()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def state(self, task, status):
        return self.store.change(
            task.id,
            task.revision,
            {task.status},
            lambda row: setattr(row, "status", status),
        )

    def test_persistence_and_history_pagination(self):
        one = self.store.create("摄影需求一")
        two = self.store.create("摄影需求二")
        one = self.state(one, "completed")
        self.store.change(
            one.id,
            one.revision,
            {"completed"},
            lambda row: setattr(
                row,
                "result",
                PlanningResult(status="completed", metrics={"model_attempts": 2}),
            ),
        )
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.get(one.id).result.metrics["model_attempts"], 2)
        self.assertEqual(self.store.list(limit=1)[0].id, two.id)
        self.assertEqual(self.store.list(limit=1, offset=1)[0].id, one.id)
        self.assertNotIn("request", self.store.list()[0].model_dump())

    def test_restart_only_interrupts_active_work_and_never_replays_it(self):
        records = []
        for status in [
            "extracting",
            "running",
            "awaiting_origin",
            "awaiting_confirmation",
            "ready",
            "completed",
            "cancelled",
        ]:
            task = self.store.create(status)
            task = self.state(task, status)
            records.append(task)
        self.store.close()
        self.store.open()
        for previous in records:
            current = self.store.get(previous.id)
            if previous.status in {"extracting", "running", "awaiting_origin"}:
                self.assertEqual(current.status, "interrupted")
                self.assertEqual(current.error_code, "server_restarted")
                self.assertEqual(current.revision, previous.revision + 1)
            else:
                self.assertEqual(current.status, previous.status)
                self.assertEqual(current.revision, previous.revision)

    def test_stale_revision_and_failed_mutation_are_atomic(self):
        task = self.store.create("私人需求")
        current = self.state(task, "awaiting_confirmation")
        with self.assertRaisesRegex(TaskError, "revision_conflict"):
            self.store.change(
                task.id,
                task.revision,
                {"awaiting_confirmation"},
                lambda row: setattr(row, "request", "stale"),
            )

        def fail(row):
            row.request = "should roll back"
            raise TaskError("invalid_request")

        with self.assertRaises(TaskError):
            self.store.change(
                task.id, current.revision, {"awaiting_confirmation"}, fail
            )
        self.assertEqual(self.store.get(task.id).request, "私人需求")
        self.assertEqual(self.store.get(task.id).revision, current.revision)

    def test_idempotency_key_is_bound_to_exact_request(self):
        task = self.store.create("同一请求", "same-key")
        self.assertEqual(self.store.find_idempotent("same-key", "同一请求").id, task.id)
        with self.assertRaisesRegex(TaskError, "idempotency_key_conflict"):
            self.store.find_idempotent("same-key", "另一请求")

    def test_sql_parameters_do_not_execute_user_text(self):
        task = self.store.create("'); DROP TABLE tasks; --")
        self.assertEqual(self.store.get(task.id).request, "'); DROP TABLE tasks; --")
        with self.assertRaises(TaskError):
            self.store.get("' OR 1=1 --")
        self.assertEqual(len(self.store.list()), 1)

    def test_second_server_cannot_interrupt_the_first_servers_tasks(self):
        task = self.store.create("active")
        other = TaskStore(self.path)
        with self.assertRaisesRegex(RuntimeError, "database_in_use"):
            other.open()
        self.assertEqual(self.store.get(task.id).status, "extracting")
        other.close()

    def test_unknown_database_version_is_not_rewritten(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(RuntimeError, "unsupported_database_version"):
            self.store.open()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 99
            )
