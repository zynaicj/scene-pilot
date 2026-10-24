"""Small SQLite store with atomic revision checks and one server owner per file."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from scene_pilot.api.models import ACTIVE, TERMINAL, TaskError, TaskRecord, TaskEvent
from scene_pilot.core.progress import TaskProgress

EVENT_RETENTION = 256


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._lock_file = None

    def open(self):
        if self._lock_file is not None:
            raise RuntimeError("store_already_open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("database_in_use_single_worker_required") from None
        self._lock_file = lock
        try:
            with self._connect(write=True) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise RuntimeError("unsupported_database_version")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "idempotency_key TEXT UNIQUE, record TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at, id)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS task_events ("
                    "task_id TEXT NOT NULL, sequence INTEGER NOT NULL, record TEXT NOT NULL, "
                    "PRIMARY KEY(task_id, sequence))"
                )
                rows = connection.execute("SELECT record FROM tasks").fetchall()
                for row in rows:
                    task = TaskRecord.model_validate_json(row[0])
                    active = task.status in ACTIVE
                    if active:
                        task.status = "interrupted"
                        task.error_code = "server_restarted"
                    if version < 2 or active or not task.settled:
                        task.settled = True
                        self._save(connection, task, bump_revision=active)
                connection.execute("PRAGMA user_version = 2")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock_file is not None:
            self._lock_file.close()  # OS releases the process lock, including after a crash.
            self._lock_file = None

    @contextmanager
    def _connect(self, *, write=False):
        if self._lock_file is None:
            raise RuntimeError("store_not_open")
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _save(connection, task, *, bump_revision=True):
        if bump_revision:
            task.revision += 1
        task.updated_at = datetime.now(UTC)
        if task.status != "running":
            task.progress = TaskProgress(phase=task.status)
        task.event_sequence += 1
        connection.execute(
            "UPDATE tasks SET status=?, record=? WHERE id=?",
            (task.status, task.model_dump_json(exclude_computed_fields=True), task.id),
        )
        TaskStore._append_event(connection, task)

    @staticmethod
    def _append_event(connection, task):
        event = TaskEvent.from_record(task)
        connection.execute(
            "INSERT INTO task_events(task_id,sequence,record) VALUES(?,?,?)",
            (
                task.id,
                task.event_sequence,
                event.model_dump_json(exclude_computed_fields=True),
            ),
        )
        connection.execute(
            "DELETE FROM task_events WHERE task_id=? AND sequence<=?",
            (task.id, task.event_sequence - EVENT_RETENTION),
        )

    def get(self, task_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskError("task_not_found", 404)
        return TaskRecord.model_validate_json(row[0])

    def list(self, *, limit=20, offset=0):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record FROM tasks ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [TaskRecord.model_validate_json(row[0]).summary() for row in rows]

    def find_idempotent(self, key, request):
        if key is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        task = TaskRecord.model_validate_json(row[0])
        if task.request != request:
            raise TaskError("idempotency_key_conflict")
        return task

    def create(self, request, key=None):
        now = datetime.now(UTC)
        task = TaskRecord(
            id=uuid4().hex,
            request=request,
            status="extracting",
            revision=1,
            created_at=now,
            updated_at=now,
            settled=False,
            event_sequence=1,
        )
        with self._connect(write=True) as connection:
            connection.execute(
                "INSERT INTO tasks(id,status,created_at,idempotency_key,record) VALUES(?,?,?,?,?)",
                (
                    task.id,
                    task.status,
                    now.isoformat(),
                    key,
                    task.model_dump_json(exclude_computed_fields=True),
                ),
            )
            self._append_event(connection, task)
        return task

    def change(self, task_id, revision, states, mutate):
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT record FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskError("task_not_found", 404)
            task = TaskRecord.model_validate_json(row[0])
            if revision is not None and task.revision != revision:
                raise TaskError("revision_conflict")
            if task.status not in states:
                raise TaskError("invalid_task_state")
            mutate(task)
            self._save(connection, task)
        return task

    def interrupt_active(self):
        with self._connect(write=True) as connection:
            rows = connection.execute(
                "SELECT record FROM tasks WHERE status IN ('extracting','running','awaiting_origin')"
            ).fetchall()
            for row in rows:
                task = TaskRecord.model_validate_json(row[0])
                if task.status in ACTIVE:
                    task.status = "interrupted"
                    task.error_code = "server_stopped"
                    self._save(connection, task)

    def progress(self, task_id, progress):
        # Progress has its own event sequence; it must not invalidate user revisions.
        task = self.get(task_id)
        if task.status != "running" or task.progress == progress:
            return
        task.progress = TaskProgress.model_validate(progress)
        with self._connect(write=True) as connection:
            self._save(connection, task, bump_revision=False)

    def settle(self, task_id):
        task = self.get(task_id)
        if task.settled or task.status not in TERMINAL:
            return
        task.settled = True
        with self._connect(write=True) as connection:
            self._save(connection, task, bump_revision=False)

    def events_after(self, task_id, after=None):
        task = self.get(task_id)
        snapshot = TaskEvent.from_record(task)
        with self._connect() as connection:
            oldest = connection.execute(
                "SELECT MIN(sequence) FROM task_events WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if after is None:
                return "snapshot", [snapshot], snapshot
            if after > task.event_sequence or oldest is None or after < oldest - 1:
                return "reset", [snapshot], snapshot
            rows = connection.execute(
                "SELECT record FROM task_events WHERE task_id=? AND sequence>? "
                "ORDER BY sequence",
                (task_id, after),
            ).fetchall()
        return (
            "update",
            [TaskEvent.model_validate_json(row[0]) for row in rows],
            snapshot,
        )
