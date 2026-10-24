"""Single-process task lifecycle; external calls are never retried by HTTP handlers."""

import asyncio

from scene_pilot.api.models import ACTIVE, PlanningResult, TaskError
from scene_pilot.core.confirmation import ConfirmedBrief, confirm_brief
from scene_pilot.core.execution import ExecutionLimits
from scene_pilot.core.photography import PhotographyBrief
from scene_pilot.core.progress import TaskProgress


class TaskService:
    def __init__(
        self, store, runtime, *, limits=None, max_active=2, origin_wait_seconds=900
    ):
        self.store = store
        self.runtime = runtime
        self.limits = limits or ExecutionLimits()
        self.max_active = max_active
        self.origin_wait_seconds = origin_wait_seconds
        self.jobs: dict[str, asyncio.Task] = {}
        self.choices: dict[str, asyncio.Future] = {}
        self.closing = False

    def _capacity(self):
        if self.closing:
            raise TaskError("server_stopping", 503)
        if sum(not job.done() for job in self.jobs.values()) >= self.max_active:
            raise TaskError("server_busy", 503)

    def _spawn(self, task_id, coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs[task_id] = job

        def finished(task):
            if self.jobs.get(task_id) is task:
                self.jobs.pop(task_id, None)
                self.store.settle(task_id)
                choice = self.choices.pop(task_id, None)
                if choice is not None and not choice.done():
                    choice.cancel()
            if not task.cancelled():
                task.exception()  # Workers sanitize errors; do not log task exception payloads.

        job.add_done_callback(finished)

    def create(self, request, key=None):
        existing = self.store.find_idempotent(key, request)
        if existing is not None:
            return existing
        if len(request) > self.limits.max_input_chars:
            raise TaskError("request_too_long", 422)
        self._capacity()
        task = self.store.create(request, key)
        self._spawn(task.id, self._extract(task.id, request))
        return task

    async def _extract(self, task_id, request):
        try:
            extracted = await self.runtime.extract(request)

            def save(task):
                task.draft = extracted.draft
                task.usage = extracted.usage
                task.error_code = extracted.error_code
                task.status = "awaiting_confirmation"
                task.settled = True

            self.store.change(task_id, None, {"extracting"}, save)
        except asyncio.CancelledError:
            pass  # Cancel/shutdown committed its terminal state before cancelling the job.
        except TaskError:
            pass  # A user cancellation won the race.
        except Exception:
            self._finish(
                task_id, PlanningResult(status="failed", error_code="extraction_failed")
            )

    def update_brief(self, task_id, body):
        def update(task):
            if (
                len(task.request) + len(body.brief.model_dump_json())
                > self.limits.max_input_chars
            ):
                raise TaskError("request_too_long", 422)
            task.draft = body.brief

        return self.store.change(
            task_id, body.revision, {"awaiting_confirmation"}, update
        )

    def confirm(self, task_id, body):
        def accept(task):
            try:
                brief = PhotographyBrief.model_validate(
                    task.draft.model_dump() if task.draft else {}
                )
            except ValueError:
                raise TaskError("complete_brief_required", 422) from None
            if (
                len(task.request) + len(brief.model_dump_json())
                > self.limits.max_input_chars
            ):
                raise TaskError("request_too_long", 422)
            confirmed = confirm_brief(
                task.request,
                brief,
                accepted=body.accepted,
                preparation_usage=task.usage,
            )
            task.confirmed_brief = confirmed.brief
            task.confirmation_id = confirmed.confirmation_id
            task.confirmed_at = confirmed.confirmed_at
            task.error_code = None
            task.status = "ready"

        return self.store.change(
            task_id, body.revision, {"awaiting_confirmation"}, accept
        )

    def start(self, task_id, revision):
        self._capacity()

        def start(task):
            if not task.confirmed_brief or not task.confirmation_id:
                raise TaskError("confirmation_required")
            task.status = "running"
            task.settled = False
            task.progress = TaskProgress(phase="loading")

        task = self.store.change(task_id, revision, {"ready"}, start)
        self._spawn(task_id, self._plan(task))
        return task

    async def _select_origin(self, task_id, options):
        future = asyncio.get_running_loop().create_future()
        self.choices[task_id] = future

        def waiting(task):
            task.origin_options = list(options)
            task.status = "awaiting_origin"

        self.store.change(task_id, None, {"running"}, waiting)
        try:
            return await asyncio.wait_for(future, timeout=self.origin_wait_seconds)
        except TimeoutError:
            self._finish(
                task_id,
                PlanningResult(
                    status="interrupted", error_code="origin_selection_expired"
                ),
                settled=False,
            )
            raise
        finally:
            self.choices.pop(task_id, None)

    def select_origin(self, task_id, body):
        future = self.choices.get(task_id)
        if future is None or future.done():
            # Still look up the task to preserve consistent 404 semantics.
            self.store.get(task_id)
            raise TaskError("origin_selection_not_pending")

        def select(task):
            ids = {item.evidence_id for item in task.origin_options}
            if not body.skip and body.evidence_id not in ids:
                raise TaskError("origin_not_in_this_task", 422)
            task.selected_origin_id = body.evidence_id
            task.origin_skipped = body.skip
            task.status = "running"
            task.progress = TaskProgress(phase="planning")

        task = self.store.change(task_id, body.revision, {"awaiting_origin"}, select)
        future.set_result(body.evidence_id)
        return task

    async def _plan(self, task):
        confirmed = ConfirmedBrief(
            request=task.request,
            _brief_json=task.confirmed_brief.model_dump_json(),
            confirmation_id=task.confirmation_id,
            confirmed_at=task.confirmed_at,
            preparation_usage=task.usage,
        )

        async def choose(options):
            return await self._select_origin(task.id, options)

        try:
            result = await self.runtime.plan(
                task.request,
                confirmed,
                choose,
                on_progress=lambda progress: self.store.progress(task.id, progress),
            )
            self._finish(task.id, result)
        except asyncio.CancelledError:
            pass
        except Exception:
            self._finish(
                task.id, PlanningResult(status="failed", error_code="planning_failed")
            )

    def _finish(self, task_id, result, *, settled=True):
        def finish(task):
            task.settled = settled
            if task.status in {"cancelled", "interrupted"}:
                task.result = result.model_copy(
                    update={"status": task.status, "error_code": task.error_code}
                )
            else:
                task.status = result.status
                task.error_code = result.error_code
                task.result = result

        try:
            self.store.change(
                task_id, None, ACTIVE | {"cancelled", "interrupted"}, finish
            )
        except TaskError:
            pass

    def cancel(self, task_id, revision):
        def cancel(task):
            task.status = "cancelled"
            task.error_code = None

        task = self.store.change(
            task_id, revision, ACTIVE | {"ready", "awaiting_confirmation"}, cancel
        )
        job = self.jobs.get(task_id)
        if job is not None:
            job.cancel()
        return task

    async def close(self):
        self.closing = True
        self.store.interrupt_active()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()
        self.choices.clear()
