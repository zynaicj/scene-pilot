"""Bounded, read-only SSE subscriptions backed by the persisted event journal."""

import asyncio
import json
import re

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse

from scene_pilot.api.errors import error_payload
from scene_pilot.api.models import TERMINAL, TaskError


def cursor_sequence(task_id, cursor):
    if cursor is None or cursor == "":
        return None
    match = re.fullmatch(r"([0-9a-f]{32}):([0-9]{1,16})", cursor)
    if match is None or match[1] != task_id:
        raise TaskError("invalid_event_cursor", 422)
    return int(match[2])


def frame(kind, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id else ""
    return (
        prefix
        + f"event: {kind}\ndata: "
        + json.dumps(data, ensure_ascii=False)
        + "\n\n"
    )


class EventResponse(StreamingResponse):
    def __init__(self, hub, content):
        self.hub = hub
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            async with asyncio.timeout(self.hub.send_timeout):
                await send(message)

        try:
            await super().__call__(scope, receive, bounded_send)
        except (TimeoutError, ClientDisconnect, OSError):
            pass
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                self.hub.connections -= 1


class EventStreams:
    def __init__(
        self,
        service,
        *,
        max_connections=16,
        poll_seconds=0.25,
        heartbeat_seconds=15,
        send_timeout=15,
    ):
        self.service = service
        self.max_connections = max_connections
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.send_timeout = send_timeout
        self.connections = 0

    def response(self, task_id, cursor=None):
        after = cursor_sequence(task_id, cursor)
        if self.service.closing:
            raise TaskError("server_stopping", 503)
        # Validate before committing HTTP 200; reading never starts a worker.
        kind, events, snapshot = self.service.store.events_after(task_id, after)
        if (
            after == snapshot.sequence
            and snapshot.status in TERMINAL
            and snapshot.settled
        ):
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        if self.connections >= self.max_connections:
            raise TaskError("event_stream_limit", 503)
        self.connections += 1
        return EventResponse(
            self, self._stream(task_id, after, (kind, events, snapshot))
        )

    async def _stream(self, task_id, after, initial):
        loop = asyncio.get_running_loop()
        last_send = loop.time()
        yield "retry: 2000\n\n"
        try:
            while True:
                if self.service.closing:
                    yield frame("stream_closed", error_payload("server_stopping"))
                    return
                kind, events, snapshot = initial or self.service.store.events_after(
                    task_id, after
                )
                initial = None
                for event in events:
                    yield frame(
                        kind,
                        event.model_dump(mode="json"),
                        f"{task_id}:{event.sequence}",
                    )
                    after = event.sequence
                    last_send = loop.time()
                if snapshot.status in TERMINAL and snapshot.settled:
                    yield frame(
                        "end",
                        {
                            "task_id": task_id,
                            "status": snapshot.status,
                            "result_available": snapshot.result_available,
                        },
                    )
                    return
                if loop.time() - last_send >= self.heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    last_send = loop.time()
                await asyncio.sleep(self.poll_seconds)
        except Exception:
            # Headers have been sent; expose a fixed connection error, never raw exceptions.
            yield frame("stream_error", error_payload("event_stream_unavailable"))
