"""Loopback-only HTTP smoke test: real Uvicorn, offline model/map runtime."""

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

import httpx
import uvicorn

from scene_pilot.api.app import create_app
from test_api import OfflineRuntime


class SSEHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_http_delivery_disconnect_and_bounded_shutdown(self):
        async def next_event(lines):
            event = {}
            async with asyncio.timeout(3):
                async for line in lines:
                    if not line and "data" in event:
                        event["data"] = json.loads(event["data"])
                        return event
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        event[key] = value
            self.fail("Stream ended before event")

        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.setblocking(False)
            port = listener.getsockname()[1]
            runtime = OfflineRuntime()
            app = create_app(Path(directory) / "tasks.sqlite3", runtime=runtime)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    log_config=None,
                    log_level="critical",
                    access_log=False,
                    timeout_graceful_shutdown=0.05,
                )
            )
            worker = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(3):
                    while not server.started:
                        if worker.done():
                            await worker
                            self.fail("Server stopped during startup")
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False
                ) as client:
                    created = (
                        await client.post("/tasks", json={"request": "离线HTTP测试"})
                    ).json()
                    task_id = created["id"]
                    async with asyncio.timeout(3):
                        while True:
                            task = (await client.get(f"/tasks/{task_id}")).json()
                            if task["status"] == "awaiting_confirmation":
                                break
                            await asyncio.sleep(0.01)
                    async with client.stream(
                        "GET", f"/tasks/{task_id}/events"
                    ) as response:
                        self.assertEqual(response.status_code, 200)
                        lines = response.aiter_lines()
                        snapshot = await next_event(lines)
                        self.assertEqual(snapshot["event"], "snapshot")
                        await client.post(
                            f"/tasks/{task_id}/confirm",
                            json={
                                "revision": task["revision"],
                                "accepted": True,
                            },
                        )
                        update = await next_event(lines)
                        self.assertEqual(update["event"], "update")
                        self.assertEqual(update["data"]["status"], "ready")
                    async with asyncio.timeout(3):
                        while app.state.event_streams.connections:
                            await asyncio.sleep(0.01)
                    # Keep a real subscription open during shutdown, as a browser would.
                    async with client.stream(
                        "GET", f"/tasks/{task_id}/events"
                    ) as response:
                        await next_event(response.aiter_lines())
                        server.should_exit = True
                        await asyncio.wait_for(worker, 3)
                    self.assertEqual(app.state.event_streams.connections, 0)
                    self.assertEqual(runtime.extract_calls, 1)
                    self.assertEqual(runtime.plan_calls, 0)
            finally:
                server.should_exit = True
                if not worker.done():
                    await asyncio.wait_for(worker, 3)
