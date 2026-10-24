"""HTTP workflow and asynchronous lifecycle using offline runtime fixtures."""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scene_pilot.api.app import create_app
from scene_pilot.api.models import PlanningResult
from scene_pilot.briefing import DraftResult
from scene_pilot.core.confirmation import (
    BriefDraft,
    DepartureEvidence,
    PreparationUsage,
)


class OfflineRuntime:
    def __init__(self):
        self.extract_calls = 0
        self.plan_calls = 0
        self.extract_gate = None
        self.plan_gate = None
        self.fail_extract = False
        self.fail_plan = False
        self.cancelled = asyncio.Event()
        self.selections = []
        self.confirmations = []

    async def extract(self, request):
        self.extract_calls += 1
        if self.extract_gate:
            await self.extract_gate.wait()
        if self.fail_extract:
            raise RuntimeError("https://provider/?key=private-secret")
        return DraftResult(
            BriefDraft(
                city="杭州",
                photography_style="日系",
                departure="杭州东站西广场",
                transport_mode="驾车",
                max_travel_minutes=40,
            ),
            PreparationUsage(model_attempts=1, input_tokens=10, output_tokens=5),
        )

    async def plan(self, request, confirmed, select_origin, *, on_progress=None):
        self.plan_calls += 1
        self.confirmations.append(confirmed)
        try:
            if self.fail_plan:
                raise RuntimeError("https://provider/?key=private-secret")
            if self.plan_gate:
                await self.plan_gate.wait()
            if confirmed.brief.departure and confirmed.brief.transport_mode == "驾车":
                selected = await select_origin(
                    [
                        DepartureEvidence(
                            evidence_id="origin_" + confirmed.confirmation_id,
                            retrieved_at=datetime.now(UTC),
                            query_address=confirmed.brief.departure,
                            query_city=confirmed.brief.city,
                            location="120,30",
                            formatted_address="地图出发位置",
                        )
                    ]
                )
                self.selections.append(selected)
            return PlanningResult(status="completed", metrics={"model_attempts": 2})
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.runtime = OfflineRuntime()
        self.app = create_app(self.path, runtime=self.runtime)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://localhost",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.directory.cleanup()

    async def wait_status(self, task_id, *statuses):
        async with asyncio.timeout(2):
            while True:
                response = await self.client.get(f"/tasks/{task_id}")
                self.assertEqual(response.status_code, 200, response.text)
                task = response.json()
                if task["status"] in statuses:
                    return task
                await asyncio.sleep(0.002)

    async def create(self, **kwargs):
        response = await self.client.post(
            "/tasks", json={"request": "杭州日系，单程驾车不超过 40 分钟"}, **kwargs
        )
        self.assertEqual(response.status_code, 202, response.text)
        return await self.wait_status(response.json()["id"], "awaiting_confirmation")

    async def ready(self):
        task = await self.create()
        response = await self.client.post(
            f"/tasks/{task['id']}/confirm",
            json={"revision": task["revision"], "accepted": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def start(self, task):
        response = await self.client.post(
            f"/tasks/{task['id']}/start", json={"revision": task["revision"]}
        )
        self.assertEqual(response.status_code, 202, response.text)
        return await self.wait_status(task["id"], "awaiting_origin")

    async def test_health_and_openapi_do_not_use_providers(self):
        self.assertEqual((await self.client.get("/health")).json()["version"], "0.7.0")
        schema = (await self.client.get("/openapi.json")).json()
        self.assertIn("/tasks/{task_id}/origin", schema["paths"])
        self.assertEqual(self.runtime.extract_calls, 0)
        self.assertEqual(self.runtime.plan_calls, 0)

    async def test_full_submit_edit_confirm_start_select_result_and_history(self):
        task = await self.create()
        self.assertEqual(self.runtime.plan_calls, 0)
        brief = {
            **task["draft"],
            "max_travel_minutes": 30,
            "shooting_date": "2026-11-05",
        }
        response = await self.client.patch(
            f"/tasks/{task['id']}/brief",
            json={"revision": task["revision"], "brief": brief},
        )
        self.assertEqual(response.status_code, 200)
        edited = response.json()
        response = await self.client.post(
            f"/tasks/{task['id']}/confirm",
            json={"revision": edited["revision"], "accepted": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runtime.plan_calls, 0)
        waiting = await self.start(response.json())
        choice = waiting["origin_options"][0]["evidence_id"]
        response = await self.client.post(
            f"/tasks/{task['id']}/origin",
            json={"revision": waiting["revision"], "evidence_id": choice},
        )
        self.assertEqual(response.status_code, 202)
        await self.wait_status(task["id"], "completed")
        result = (await self.client.get(f"/tasks/{task['id']}/result")).json()
        self.assertEqual(result["result"]["metrics"]["model_attempts"], 2)
        self.assertEqual(self.runtime.confirmations[0].brief.max_travel_minutes, 30)
        self.assertEqual(
            self.runtime.confirmations[0].brief.shooting_date.isoformat(), "2026-11-05"
        )
        self.assertEqual(
            self.runtime.confirmations[0].preparation_usage.model_attempts, 1
        )
        self.assertEqual(self.runtime.selections, [choice])
        history = (await self.client.get("/tasks?limit=1")).json()
        self.assertEqual(history[0]["id"], task["id"])
        self.assertNotIn("request", history[0])
        self.assertNotIn("trace_path", str(result))

    async def test_confirmation_is_explicit_and_precedes_planning(self):
        task = await self.create()
        base = f"/tasks/{task['id']}"
        self.assertEqual(
            (
                await self.client.post(
                    base + "/start", json={"revision": task["revision"]}
                )
            ).status_code,
            409,
        )
        for accepted in [False, 1, "true", None]:
            response = await self.client.post(
                base + "/confirm",
                json={"revision": task["revision"], "accepted": accepted},
            )
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runtime.plan_calls, 0)

    async def test_stale_confirmation_and_edit_after_confirmation_are_rejected(self):
        task = await self.create()
        base = f"/tasks/{task['id']}"
        edited = (
            await self.client.patch(
                base + "/brief",
                json={
                    "revision": task["revision"],
                    "brief": {**task["draft"], "max_travel_minutes": 20},
                },
            )
        ).json()
        self.assertEqual(
            (
                await self.client.post(
                    base + "/confirm",
                    json={"revision": task["revision"], "accepted": True},
                )
            ).status_code,
            409,
        )
        ready = (
            await self.client.post(
                base + "/confirm",
                json={"revision": edited["revision"], "accepted": True},
            )
        ).json()
        self.assertEqual(
            (
                await self.client.patch(
                    base + "/brief",
                    json={"revision": ready["revision"], "brief": task["draft"]},
                )
            ).status_code,
            409,
        )
        self.assertEqual(ready["confirmed_brief"]["max_travel_minutes"], 20)

    async def test_missing_core_fields_cannot_be_confirmed(self):
        task = await self.create()
        edited = (
            await self.client.patch(
                f"/tasks/{task['id']}/brief",
                json={"revision": task["revision"], "brief": {}},
            )
        ).json()
        response = await self.client.post(
            f"/tasks/{task['id']}/confirm",
            json={"revision": edited["revision"], "accepted": True},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "complete_brief_required")

    async def test_repeated_create_and_start_do_not_repeat_calls(self):
        task = await self.create(headers={"Idempotency-Key": "client-action-1"})
        second = await self.client.post(
            "/tasks",
            headers={"Idempotency-Key": "client-action-1"},
            json={"request": task["request"]},
        )
        self.assertEqual(second.json()["id"], task["id"])
        self.assertEqual(self.runtime.extract_calls, 1)
        conflict = await self.client.post(
            "/tasks",
            headers={"Idempotency-Key": "client-action-1"},
            json={"request": "other"},
        )
        self.assertEqual(conflict.status_code, 409)
        ready = (
            await self.client.post(
                f"/tasks/{task['id']}/confirm",
                json={"revision": task["revision"], "accepted": True},
            )
        ).json()
        await self.start(ready)
        again = await self.client.post(
            f"/tasks/{task['id']}/start", json={"revision": ready["revision"]}
        )
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.runtime.plan_calls, 1)

    async def test_origin_from_another_task_or_supplied_coordinates_are_rejected(self):
        first = await self.start(await self.ready())
        second = await self.start(await self.ready())
        base = f"/tasks/{first['id']}/origin"
        wrong = await self.client.post(
            base,
            json={
                "revision": first["revision"],
                "evidence_id": second["origin_options"][0]["evidence_id"],
            },
        )
        self.assertEqual(wrong.status_code, 422)
        injected = await self.client.post(
            base,
            json={"revision": first["revision"], "skip": True, "location": "120,30"},
        )
        self.assertEqual(injected.status_code, 422)
        both = await self.client.post(
            base,
            json={
                "revision": first["revision"],
                "skip": True,
                "evidence_id": first["origin_options"][0]["evidence_id"],
            },
        )
        self.assertEqual(both.status_code, 422)
        skipped = await self.client.post(
            base, json={"revision": first["revision"], "skip": True}
        )
        self.assertEqual(skipped.status_code, 202)
        await self.wait_status(first["id"], "completed")
        self.assertEqual(self.runtime.selections, [None])

    async def test_cancellation_of_waiting_origin_stops_work(self):
        task = await self.start(await self.ready())
        response = await self.client.post(
            f"/tasks/{task['id']}/cancel", json={"revision": task["revision"]}
        )
        self.assertEqual(response.status_code, 202)
        await asyncio.wait_for(self.runtime.cancelled.wait(), 1)
        current = await self.wait_status(task["id"], "cancelled")
        response = await self.client.post(
            f"/tasks/{task['id']}/origin",
            json={"revision": current["revision"], "skip": True},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.runtime.selections, [])

    async def test_capacity_is_bounded_before_starting_external_work(self):
        self.runtime.extract_gate = asyncio.Event()
        for _ in range(2):
            self.assertEqual(
                (
                    await self.client.post("/tasks", json={"request": "需求"})
                ).status_code,
                202,
            )
        response = await self.client.post("/tasks", json={"request": "第三个"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len((await self.client.get("/tasks")).json()), 2)

    async def test_safe_provider_errors_and_validation_errors(self):
        self.runtime.fail_extract = True
        response = await self.client.post("/tasks", json={"request": "需求"})
        task = await self.wait_status(response.json()["id"], "failed")
        self.assertEqual(task["error_code"], "extraction_failed")
        self.assertNotIn("private-secret", str(task))
        response = await self.client.post(
            "/tasks", json={"request": "需求", "api_key": "private-secret"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-secret", response.text)
        self.assertNotIn(b"private-secret", self.path.read_bytes())

    async def test_planning_failure_is_stored_without_provider_text(self):
        ready = await self.ready()
        self.runtime.fail_plan = True
        await self.client.post(
            f"/tasks/{ready['id']}/start", json={"revision": ready["revision"]}
        )
        task = await self.wait_status(ready["id"], "failed")
        self.assertEqual(task["error_code"], "planning_failed")
        self.assertNotIn(b"private-secret", self.path.read_bytes())

    async def test_pending_result_and_unknown_task(self):
        task = await self.create()
        self.assertEqual(
            (await self.client.get(f"/tasks/{task['id']}/result")).status_code, 409
        )
        self.assertEqual((await self.client.get("/tasks/" + "0" * 32)).status_code, 404)
        self.assertEqual((await self.client.get("/tasks/not-an-id")).status_code, 422)
        self.assertEqual((await self.client.get("/tasks?limit=101")).status_code, 422)

    async def test_input_body_limit_also_applies_without_content_length(self):
        response = await self.client.post("/tasks", json={"request": "x" * 12001})
        self.assertEqual(response.status_code, 422)

        async def body():
            yield b'{"request":"'
            yield b"x" * 70000
            yield b'"}'

        response = await self.client.post(
            "/tasks", content=body(), headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.runtime.extract_calls, 0)

    async def test_host_rebinding_and_form_submission_are_rejected(self):
        response = await self.client.get(
            "/tasks", headers={"Host": "untrusted.example"}
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/tasks", data={"request": "需求"})
        self.assertEqual(response.status_code, 422)

    async def test_shutdown_marks_inflight_as_interrupted_and_stops_workers(self):
        task = await self.start(await self.ready())
        await self.app.state.service.close()
        await asyncio.wait_for(self.runtime.cancelled.wait(), 1)
        saved = self.app.state.service.store.get(task["id"])
        self.assertEqual(saved.status, "interrupted")
        self.assertEqual(saved.error_code, "server_stopped")
        self.assertFalse(self.app.state.service.jobs)

    async def test_origin_wait_expiry_never_auto_selects_or_continues(self):
        self.app.state.service.origin_wait_seconds = 0.02
        task = await self.start(await self.ready())
        stopped = await self.wait_status(task["id"], "interrupted")
        self.assertEqual(stopped["error_code"], "origin_selection_expired")
        self.assertEqual(self.runtime.selections, [])

    async def test_app_restart_preserves_confirmations_usage_and_completed_results(
        self,
    ):
        draft = await self.create()
        ready = await self.ready()
        waiting = await self.start(await self.ready())
        completed = await self.start(await self.ready())
        await self.client.post(
            f"/tasks/{completed['id']}/origin",
            json={"revision": completed["revision"], "skip": True},
        )
        await self.wait_status(completed["id"], "completed")
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.runtime = OfflineRuntime()
        self.app = create_app(self.path, runtime=self.runtime)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.assertEqual(self.runtime.extract_calls, 0)
        self.assertEqual(self.runtime.plan_calls, 0)
        restored = await self.wait_status(ready["id"], "ready")
        self.assertEqual(restored["confirmation_id"], ready["confirmation_id"])
        self.assertEqual(restored["revision"], ready["revision"])
        await self.start(restored)
        self.assertEqual(
            self.runtime.confirmations[0].preparation_usage.model_attempts, 1
        )
        response = await self.client.post(
            f"/tasks/{draft['id']}/confirm",
            json={"revision": draft["revision"], "accepted": True},
        )
        self.assertEqual(response.status_code, 200)
        interrupted = await self.wait_status(waiting["id"], "interrupted")
        for action, body in [("start", {}), ("origin", {"skip": True})]:
            response = await self.client.post(
                f"/tasks/{waiting['id']}/{action}",
                json={"revision": interrupted["revision"], **body},
            )
            self.assertEqual(response.status_code, 409)
        result = await self.client.get(f"/tasks/{completed['id']}/result")
        self.assertEqual(result.json()["result"]["status"], "completed")

    async def test_immediate_cancellation_prevents_worker_calls_and_releases_capacity(
        self,
    ):
        service = self.app.state.service
        task = service.create("还未开始的需求")
        service.cancel(task.id, task.revision)
        await asyncio.gather(*service.jobs.values(), return_exceptions=True)
        self.assertEqual(self.runtime.extract_calls, 0)
        self.assertEqual(service.store.get(task.id).status, "cancelled")
        await self.create()
