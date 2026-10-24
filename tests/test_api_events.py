"""Actual ASGI streaming, including incremental delivery and disconnected clients."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from scene_pilot.api.app import create_app
from scene_pilot.api.models import PlanningResult
from scene_pilot.core.progress import TaskProgress
from test_api import OfflineRuntime


class StreamClient:
    """ASGI probe that observes chunks before completion (httpx buffers ASGI streams)."""

    def __init__(self, app, task_id, *, cursor=None, block_send=False, spec="2.3"):
        self.app, self.task_id = app, task_id
        self.disconnected = asyncio.Event()
        self.headers = asyncio.get_running_loop().create_future()
        self.frames = asyncio.Queue()
        self.sent_request = False
        self.cursor = cursor
        self.block_send = block_send
        self.spec = spec
        self.buffer = ""

    async def receive(self):
        if not self.sent_request:
            self.sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if self.spec == "2.4" and self.disconnected.is_set():
            raise OSError("client disconnected")
        if message["type"] == "http.response.start":
            self.headers.set_result(message)
        elif message.get("body"):
            if self.block_send:
                await asyncio.Event().wait()
            self.buffer += message["body"].decode("utf-8")
            while "\n\n" in self.buffer:
                frame, self.buffer = self.buffer.split("\n\n", 1)
                parts = dict(
                    line.split(": ", 1) for line in frame.splitlines() if ": " in line
                )
                if "data" in parts:
                    parts["data"] = json.loads(parts["data"])
                await self.frames.put(parts)

    async def open(self):
        headers = [(b"host", b"localhost")]
        if self.cursor is not None:
            headers.append((b"last-event-id", self.cursor.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": self.spec},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/tasks/{self.task_id}/events",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1234),
        }
        self.worker = asyncio.create_task(self.app(scope, self.receive, self.send))
        return await asyncio.wait_for(self.headers, 2)

    async def next(self, event):
        async with asyncio.timeout(2):
            while True:
                frame = await self.frames.get()
                if frame.get("event") == event or (
                    event == "heartbeat" and "" in frame
                ):
                    return frame

    async def close(self):
        self.disconnected.set()
        await asyncio.wait_for(self.worker, 2)


class APIEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = OfflineRuntime()
        self.app = create_app(
            Path(self.directory.name) / "tasks.sqlite3", runtime=self.runtime
        )
        self.service = self.app.state.service
        self.streams = self.app.state.event_streams
        self.streams.poll_seconds = 0.005
        self.streams.heartbeat_seconds = 0.02
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.probes = []

    async def asyncTearDown(self):
        for probe in self.probes:
            await probe.close()
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.directory.cleanup()

    async def create(self):
        response = await self.client.post(
            "/tasks", json={"request": "private-user-request"}
        )
        task_id = response.json()["id"]
        jobs = list(self.service.jobs.values())
        if jobs:
            await asyncio.gather(*jobs)
        return self.service.store.get(task_id)

    async def stream(self, task_id, **kwargs):
        probe = StreamClient(self.app, task_id, **kwargs)
        self.probes.append(probe)
        headers = await probe.open()
        self.assertEqual(headers["status"], 200)
        self.assertIn(b"text/event-stream", dict(headers["headers"])[b"content-type"])
        return probe

    async def test_initial_snapshot_live_update_heartbeat_and_no_provider_reentry(self):
        task = await self.create()
        probe = await self.stream(task.id)
        first = await probe.next("snapshot")
        self.assertEqual(first["data"]["status"], "awaiting_confirmation")
        self.assertNotIn("private-user-request", json.dumps(first))
        self.assertNotIn("杭州东站", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("request", first["data"])
        await probe.next("heartbeat")
        self.assertFalse(probe.worker.done())
        response = await self.client.post(
            f"/tasks/{task.id}/confirm",
            json={"revision": task.revision, "accepted": True},
        )
        update = await probe.next("update")
        self.assertEqual(update["data"]["status"], "ready")
        self.assertEqual(update["data"]["revision"], response.json()["revision"])
        self.assertGreater(update["data"]["sequence"], first["data"]["sequence"])
        self.assertEqual(self.runtime.extract_calls, 1)
        self.assertEqual(self.runtime.plan_calls, 0)

    async def test_disconnect_does_not_cancel_background_task(self):
        self.runtime.extract_gate = asyncio.Event()
        response = await self.client.post("/tasks", json={"request": "等待提取"})
        task_id = response.json()["id"]
        probe = await self.stream(task_id)
        await probe.next("snapshot")
        await probe.close()
        self.assertFalse(self.service.jobs[task_id].done())
        self.runtime.extract_gate.set()
        await asyncio.gather(*self.service.jobs.values())
        self.assertEqual(
            self.service.store.get(task_id).status, "awaiting_confirmation"
        )
        self.assertEqual(self.streams.connections, 0)
        self.assertEqual(self.runtime.extract_calls, 1)

    async def test_reconnect_replays_only_missed_events_and_terminal_ack_is_204(self):
        task = await self.create()
        first = await self.stream(task.id)
        snapshot = await first.next("snapshot")
        await first.close()
        ready = (
            await self.client.post(
                f"/tasks/{task.id}/confirm",
                json={"revision": task.revision, "accepted": True},
            )
        ).json()
        await self.client.post(
            f"/tasks/{task.id}/cancel", json={"revision": ready["revision"]}
        )
        second = await self.stream(task.id, cursor=snapshot["id"])
        one, two = await second.next("update"), await second.next("update")
        self.assertEqual(
            [one["data"]["status"], two["data"]["status"]], ["ready", "cancelled"]
        )
        await second.next("end")
        await asyncio.wait_for(second.worker, 1)
        response = await self.client.get(
            f"/tasks/{task.id}/events", headers={"Last-Event-ID": two["id"]}
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.runtime.extract_calls, 1)
        self.assertEqual(self.runtime.plan_calls, 0)

    async def test_refresh_query_cursor_and_header_precedence(self):
        task = await self.create()
        await self.client.post(
            f"/tasks/{task.id}/cancel", json={"revision": task.revision}
        )
        last = self.service.store.get(task.id)
        response = await self.client.get(
            f"/tasks/{task.id}/events",
            params={"after": f"{task.id}:{task.event_sequence}"},
        )
        self.assertIn("event: update", response.text)
        self.assertNotIn("event: snapshot", response.text)
        response = await self.client.get(
            f"/tasks/{task.id}/events",
            params={"after": f"{task.id}:1"},
            headers={"Last-Event-ID": f"{task.id}:{last.event_sequence}"},
        )
        self.assertEqual(response.status_code, 204)

    async def test_unknown_task_and_invalid_or_cross_task_cursors_rejected_before_stream(
        self,
    ):
        task = await self.create()
        other = await self.create()
        self.assertEqual(
            (await self.client.get("/tasks/" + "f" * 32 + "/events")).status_code, 404
        )
        for cursor in [
            "bad-secret",
            "1",
            f"{other.id}:1",
            f"{task.id}:-1",
            f"{task.id}:" + "9" * 40,
        ]:
            response = await self.client.get(
                f"/tasks/{task.id}/events", headers={"Last-Event-ID": cursor}
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("bad-secret", response.text)
            self.assertIn("message", response.json())
        self.assertEqual(self.streams.connections, 0)

    async def test_future_cursor_resets_to_authoritative_snapshot(self):
        task = await self.create()
        await self.client.post(
            f"/tasks/{task.id}/cancel", json={"revision": task.revision}
        )
        response = await self.client.get(
            f"/tasks/{task.id}/events", headers={"Last-Event-ID": f"{task.id}:999"}
        )
        self.assertIn("event: reset", response.text)
        self.assertIn('"status": "cancelled"', response.text)

    async def test_connection_limit_disconnect_and_asgi24_release_slots(self):
        self.streams.max_connections = 1
        task = await self.create()
        first = await self.stream(task.id)
        await first.next("snapshot")
        response = await self.client.get(f"/tasks/{task.id}/events")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "event_stream_limit")
        await first.close()
        second = await self.stream(task.id, spec="2.4")
        await second.next("snapshot")
        await second.close()
        self.assertEqual(self.streams.connections, 0)

    async def test_slow_network_send_times_out_without_stopping_planning(self):
        self.streams.send_timeout = 0.02
        task = await self.create()
        probe = await self.stream(task.id, block_send=True)
        await asyncio.wait_for(probe.worker, 1)
        self.assertEqual(self.streams.connections, 0)
        self.assertEqual(
            self.service.store.get(task.id).status, "awaiting_confirmation"
        )

    async def test_multiple_subscribers_receive_updates_without_cross_task_data(self):
        task, other = await self.create(), await self.create()
        first, second = await self.stream(task.id), await self.stream(task.id)
        separate = await self.stream(other.id)
        for probe in [first, second, separate]:
            await probe.next("snapshot")
        await self.client.post(
            f"/tasks/{task.id}/confirm",
            json={"revision": task.revision, "accepted": True},
        )
        one, two = await first.next("update"), await second.next("update")
        self.assertEqual(one, two)
        await separate.next("heartbeat")
        self.assertEqual(
            self.service.store.get(other.id).status, "awaiting_confirmation"
        )
        self.assertTrue(
            all(item.get("event") != "update" for item in list(separate.frames._queue))
        )

    async def test_cancellation_end_waits_for_partial_result_to_be_saved(self):
        entered, cleanup = asyncio.Event(), asyncio.Event()

        async def plan(request, confirmed, choose, *, on_progress=None):
            on_progress(TaskProgress(phase="searching"))
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup.wait()
                return PlanningResult(status="cancelled", metrics={"map_attempts": 1})

        self.runtime.plan = plan
        task = await self.create()
        ready = (
            await self.client.post(
                f"/tasks/{task.id}/confirm",
                json={"revision": task.revision, "accepted": True},
            )
        ).json()
        await self.client.post(
            f"/tasks/{task.id}/start", json={"revision": ready["revision"]}
        )
        await asyncio.wait_for(entered.wait(), 1)
        probe = await self.stream(task.id)
        initial = (await probe.next("snapshot"))["data"]
        self.assertEqual(initial["progress"]["phase"], "searching")
        await self.client.post(
            f"/tasks/{task.id}/cancel", json={"revision": initial["revision"]}
        )
        cancelled = (await probe.next("update"))["data"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["settled"])
        await probe.next("heartbeat")
        self.assertFalse(probe.worker.done())
        cleanup.set()
        final = (await probe.next("update"))["data"]
        self.assertTrue(final["settled"])
        self.assertTrue(final["result_available"])
        await probe.next("end")
        result = (await self.client.get(f"/tasks/{task.id}/result")).json()
        self.assertTrue(result["settled"])
        self.assertEqual(result["result"]["metrics"]["map_attempts"], 1)

    async def test_shutdown_closes_subscriptions_and_rejects_new_ones(self):
        task = await self.create()
        probe = await self.stream(task.id)
        await probe.next("snapshot")
        await self.service.close()
        await probe.next("stream_closed")
        await asyncio.wait_for(probe.worker, 1)
        self.assertEqual(self.streams.connections, 0)
        self.assertEqual(
            (await self.client.get(f"/tasks/{task.id}/events")).status_code, 503
        )

    async def test_midstream_storage_error_is_safe_and_releases_connection(self):
        task = await self.create()
        probe = await self.stream(task.id)
        await probe.next("snapshot")
        with patch.object(
            self.service.store,
            "events_after",
            side_effect=ValueError("private-provider-key"),
        ):
            event = await probe.next("stream_error")
            await asyncio.wait_for(probe.worker, 1)
        self.assertNotIn("private-provider-key", json.dumps(event))
        self.assertEqual(self.streams.connections, 0)

    async def test_http_and_task_errors_have_safe_readable_messages(self):
        invalid = await self.client.post(
            "/tasks", json={"request": "private", "api_key": "fake-secret"}
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertTrue(invalid.json()["message"])
        self.assertNotIn("fake-secret", invalid.text)
        self.runtime.fail_plan = True
        task = await self.create()
        ready = (
            await self.client.post(
                f"/tasks/{task.id}/confirm",
                json={"revision": task.revision, "accepted": True},
            )
        ).json()
        await self.client.post(
            f"/tasks/{task.id}/start", json={"revision": ready["revision"]}
        )
        if self.service.jobs:
            await asyncio.gather(*self.service.jobs.values())
        detail = (await self.client.get(f"/tasks/{task.id}")).json()
        self.assertEqual(detail["error_code"], "planning_failed")
        self.assertTrue(detail["error_message"])
        stream = await self.client.get(f"/tasks/{task.id}/events")
        self.assertNotIn("private-secret", stream.text)
        self.assertIn(detail["error_message"], stream.text)

    async def test_openapi_documents_stream_content_type(self):
        schema = (await self.client.get("/openapi.json")).json()
        endpoint = schema["paths"]["/tasks/{task_id}/events"]["get"]
        self.assertEqual(
            set(endpoint["responses"]["200"]["content"]), {"text/event-stream"}
        )
        self.assertIn("204", endpoint["responses"])
