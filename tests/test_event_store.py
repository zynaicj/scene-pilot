"""Event persistence, bounded replay, migrations and revision isolation."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from scene_pilot.api.models import PlanningResult, TaskRecord
from scene_pilot.api.store import EVENT_RETENTION, TaskStore
from scene_pilot.core.progress import TaskProgress


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.store.open()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def running(self):
        task = self.store.create("private-user-address")
        return self.store.change(
            task.id,
            task.revision,
            {"extracting"},
            lambda row: setattr(row, "status", "running"),
        )

    def test_progress_sequence_does_not_invalidate_revision_and_deduplicates(self):
        task = self.running()
        self.store.progress(task.id, TaskProgress(phase="searching"))
        changed = self.store.get(task.id)
        self.assertEqual(changed.revision, task.revision)
        self.assertEqual(changed.event_sequence, task.event_sequence + 1)
        self.store.progress(task.id, TaskProgress(phase="searching"))
        self.assertEqual(self.store.get(task.id).event_sequence, changed.event_sequence)
        self.store.change(
            task.id,
            task.revision,
            {"running"},
            lambda row: setattr(row, "status", "cancelled"),
        )
        self.store.progress(task.id, TaskProgress(phase="routes"))
        self.assertEqual(self.store.get(task.id).progress.phase, "cancelled")

    def test_changes_and_journal_commit_or_rollback_together(self):
        task = self.running()
        with patch.object(TaskStore, "_append_event", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.store.change(
                    task.id,
                    task.revision,
                    {"running"},
                    lambda row: setattr(row, "status", "cancelled"),
                )
        current = self.store.get(task.id)
        self.assertEqual(current.status, task.status)
        self.assertEqual(current.revision, task.revision)
        self.assertEqual(self.store.events_after(task.id, task.event_sequence)[1], [])

    def test_event_history_is_bounded_and_old_or_future_cursor_gets_reset(self):
        task = self.running()
        for index in range(EVENT_RETENTION + 3):
            self.store.progress(
                task.id, TaskProgress(phase="routes" if index % 2 else "searching")
            )
        current = self.store.get(task.id)
        oldest = current.event_sequence - EVENT_RETENTION + 1
        kind, events, _ = self.store.events_after(task.id, oldest - 1)
        self.assertEqual(kind, "update")
        self.assertEqual(len(events), EVENT_RETENTION)
        self.assertEqual(
            [item.sequence for item in events],
            list(range(oldest, current.event_sequence + 1)),
        )
        for after in [1, current.event_sequence + 1]:
            kind, events, _ = self.store.events_after(task.id, after)
            self.assertEqual(kind, "reset")
            self.assertEqual(
                [item.sequence for item in events], [current.event_sequence]
            )

    def test_event_records_never_include_user_drafts_addresses_or_result_text(self):
        task = self.running()
        self.store.change(
            task.id,
            task.revision,
            {"running"},
            lambda row: setattr(
                row,
                "result",
                PlanningResult(status="incomplete", final_text="private-result-text"),
            ),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            serialized = str(
                connection.execute("SELECT record FROM task_events").fetchall()
            )
        self.assertNotIn("private-user-address", serialized)
        self.assertNotIn("private-result-text", serialized)
        self.assertNotIn('"request"', serialized)
        self.assertNotIn('"draft"', serialized)

    def test_restart_appends_interruption_and_retains_replay(self):
        task = self.running()
        self.store.progress(task.id, TaskProgress(phase="routes"))
        self.store.close()
        self.store.open()
        kind, events, last = self.store.events_after(task.id, task.event_sequence)
        self.assertEqual(kind, "update")
        self.assertEqual(
            [item.progress.phase for item in events], ["routes", "interrupted"]
        )
        self.assertEqual(last.error_code, "server_restarted")
        self.assertTrue(last.settled)
        self.assertEqual(last.revision, task.revision + 1)

    def legacy_database(self, corrupt=False):
        self.store.close()
        path = self.path.with_name("legacy.sqlite3")
        records = []
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "created_at TEXT NOT NULL, idempotency_key TEXT UNIQUE, record TEXT NOT NULL)"
            )
            for index, status in enumerate(
                ["awaiting_confirmation", "ready", "completed", "awaiting_origin"]
            ):
                task = TaskRecord(
                    id=f"{index:032x}",
                    request="保留原有需求",
                    status=status,
                    revision=5,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    result=PlanningResult(status="completed")
                    if status == "completed"
                    else None,
                )
                data = task.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                    exclude={"progress", "settled", "event_sequence"},
                )
                connection.execute(
                    "INSERT INTO tasks VALUES(?,?,?,?,?)",
                    (
                        task.id,
                        status,
                        task.created_at.isoformat(),
                        f"key{index}",
                        "broken" if corrupt and index == 3 else json.dumps(data),
                    ),
                )
                records.append(task)
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        self.store = TaskStore(path)
        return path, records

    def test_v1_migration_keeps_existing_data_and_only_interrupts_active_work(self):
        path, records = self.legacy_database()
        self.store.open()
        for previous in records:
            current = self.store.get(previous.id)
            active = previous.status == "awaiting_origin"
            self.assertEqual(
                current.status, "interrupted" if active else previous.status
            )
            self.assertEqual(current.revision, previous.revision + int(active))
            self.assertEqual(current.request, previous.request)
            self.assertEqual(current.result, previous.result)
            self.assertEqual(current.event_sequence, 1)
            self.assertEqual(self.store.events_after(current.id)[0], "snapshot")
        self.assertEqual(
            self.store.find_idempotent("key0", records[0].request).id, records[0].id
        )
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.get(records[0].id).event_sequence, 1)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_failed_migration_rolls_back_schema_and_records(self):
        path, records = self.legacy_database(corrupt=True)
        with self.assertRaises(ValueError):
            self.store.open()
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='task_events'"
                ).fetchone()
            )
            raw = json.loads(
                connection.execute(
                    "SELECT record FROM tasks WHERE id=?", (records[0].id,)
                ).fetchone()[0]
            )
            self.assertNotIn("event_sequence", raw)
