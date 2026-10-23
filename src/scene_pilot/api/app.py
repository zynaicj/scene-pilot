"""FastAPI entry point; a single local server owns the SQLite task store."""

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from scene_pilot.api.models import (
    AcceptBrief,
    CreateTask,
    ResultView,
    Revision,
    SelectOrigin,
    TaskDetail,
    TaskError,
    TaskSummary,
    TERMINAL,
    UpdateBrief,
)
from scene_pilot.api.runtime import PlanningRuntime
from scene_pilot.api.service import TaskService
from scene_pilot.api.store import TaskStore
from scene_pilot.core.execution import ExecutionLimits


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes=65536):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                raise HTTPException(413, "request_too_large")
            return message

        await self.app(scope, limited_receive, send)


def create_app(
    database_path: Path = Path("data/scene-pilot.sqlite3"),
    *,
    runtime=None,
    limits=None,
    max_active=2,
    origin_wait_seconds=900,
) -> FastAPI:
    limits = limits or ExecutionLimits()
    store = TaskStore(database_path)
    engine = runtime or PlanningRuntime(limits, Path("logs/agent_runs"))
    service = TaskService(
        store,
        engine,
        limits=limits,
        max_active=max_active,
        origin_wait_seconds=origin_wait_seconds,
    )

    @asynccontextmanager
    async def lifespan(app):
        store.open()
        try:
            yield
        finally:
            try:
                await service.close()
            finally:
                store.close()

    app = FastAPI(title="ScenePilot", version="0.6.0", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )

    @app.exception_handler(TaskError)
    async def task_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code, content={"error_code": exc.code}
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Default validation responses echo input values; keep private input out of errors.
        return JSONResponse(status_code=422, content={"error_code": "invalid_request"})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        code = "request_too_large" if exc.status_code == 413 else "http_error"
        return JSONResponse(status_code=exc.status_code, content={"error_code": code})

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse(status_code=500, content={"error_code": "internal_error"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.6.0"}

    @app.post("/tasks", response_model=TaskDetail, status_code=202)
    async def create(
        body: CreateTask,
        idempotency_key: str | None = Header(
            default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$"
        ),
    ):
        return service.create(body.request, idempotency_key).detail()

    @app.get("/tasks", response_model=list[TaskSummary])
    async def list_tasks(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return store.list(limit=limit, offset=offset)

    @app.get("/tasks/{task_id}", response_model=TaskDetail)
    async def get_task(task_id: UUID):
        return store.get(task_id.hex).detail()

    @app.patch("/tasks/{task_id}/brief", response_model=TaskDetail)
    async def update_brief(task_id: UUID, body: UpdateBrief):
        return service.update_brief(task_id.hex, body).detail()

    @app.post("/tasks/{task_id}/confirm", response_model=TaskDetail)
    async def confirm(task_id: UUID, body: AcceptBrief):
        return service.confirm(task_id.hex, body).detail()

    @app.post("/tasks/{task_id}/start", response_model=TaskDetail, status_code=202)
    async def start(task_id: UUID, body: Revision):
        return service.start(task_id.hex, body.revision).detail()

    @app.post("/tasks/{task_id}/origin", response_model=TaskDetail, status_code=202)
    async def origin(task_id: UUID, body: SelectOrigin):
        return service.select_origin(task_id.hex, body).detail()

    @app.post("/tasks/{task_id}/cancel", response_model=TaskDetail, status_code=202)
    async def cancel(task_id: UUID, body: Revision):
        return service.cancel(task_id.hex, body.revision).detail()

    @app.get("/tasks/{task_id}/result", response_model=ResultView)
    async def result(task_id: UUID):
        task = store.get(task_id.hex)
        if task.status not in TERMINAL:
            raise TaskError("result_not_ready")
        return ResultView(
            task_id=task.id,
            status=task.status,
            result=task.result,
            error_code=task.error_code,
        )

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="ScenePilot 本机后端服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--database", type=Path, default=Path("data/scene-pilot.sqlite3")
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.database),
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
