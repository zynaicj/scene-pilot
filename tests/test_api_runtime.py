"""Exercise the HTTP adapter with the real graph/observer and offline providers."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage

from scene_pilot.api.app import create_app
from scene_pilot.api.runtime import PlanningRuntime
from scene_pilot.core.confirmation import BriefDraft
from scene_pilot.core.execution import (
    ExecutionBudgetExceeded,
    ExecutionLimits,
    current_run,
)
from scene_pilot.integrations.amap_results import compact_result
from test_agent_execution import ScriptedModel, call
from test_amap_policy import POI, wrapped_tool
from test_confirmed_routes import geocoder


class APIModel(ScriptedModel):
    def bind_tools(self, tools, **kwargs):
        if tools == [BriefDraft]:
            return self
        return super().bind_tools(tools, **kwargs)


def script(messages):
    system = messages[0].content
    results = [item for item in messages if item.type == "tool"]
    if "仅提取用户明确提供的摄影需求" in system:
        return call(
            "BriefDraft",
            {
                "city": "杭州",
                "photography_style": "日系",
                "departure": "杭州东站西广场",
                "transport_mode": "驾车",
                "max_travel_minutes": 40,
            },
            "draft",
        )
    if "你是摄影场景调研专员" in system:
        if not results:
            return call(
                "maps_text_search", {"city": "杭州", "keywords": "公园"}, "search"
            )
        return AIMessage(content=results[-1].content)
    if "你是摄影交通路线专员" in system:
        if not results:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "maps_direction_driving",
                        "args": {"origin": "119,30", "destination": f"120.{i},30"},
                        "id": f"route{i}",
                        "type": "tool_call",
                    }
                    for i in range(3)
                ],
            )
        return AIMessage(
            content=json.dumps([json.loads(item.content) for item in results])
        )
    names = [item.name for item in results]
    tasks = [item for item in results if item.name == "task"]
    if "write_todos" not in names:
        return call("write_todos", {"todos": []}, "todos")
    if len(tasks) < 2:
        return call(
            "task",
            {
                "subagent_type": "scene-agent" if not tasks else "route-agent",
                "description": "查找杭州公园；从119,30驾车到候选坐标，返回证据ID",
            },
            f"task{len(tasks)}",
        )
    pois = json.loads(tasks[0].content)["pois"]
    if "inspect_shooting_conditions" not in names:
        return call(
            "inspect_shooting_conditions",
            {"evidence_ids": [item["evidence_id"] for item in pois]},
            "conditions",
        )
    if "finalize_recommendation" not in names:
        return call(
            "finalize_recommendation",
            {
                "user_statements": [],
                "candidates": [{"evidence_id": item["evidence_id"]} for item in pois],
            },
            "final",
        )
    raise AssertionError("No extra model call after a valid final submission")


class APIRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.runtime = PlanningRuntime(ExecutionLimits(), self.path / "traces")
        self.model = APIModel(script=script)
        self.runtime._model = self.model
        self.geo, self.geo_provider = geocoder()
        self.search_calls = []
        self.route_calls = []

        async def search(**kwargs):
            self.search_calls.append(kwargs)
            return {
                "pois": [
                    {"id": f"B{i}", "name": f"测试公园{i}", "location": f"120.{i},30"}
                    for i in range(3)
                ]
            }

        async def driving(**kwargs):
            self.route_calls.append(kwargs)
            seconds = 2400 if kwargs["destination"] == "120.0,30" else 3300
            return {"paths": [{"distance": 1000, "duration": seconds}]}

        self.runtime._tools = (
            [wrapped_tool(search)],
            [self.geo, wrapped_tool(driving, "maps_direction_driving")],
            [],
        )
        self.app = create_app(self.path / "tasks.sqlite3", runtime=self.runtime)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.directory.cleanup()

    async def wait_status(self, task_id, *statuses):
        async with asyncio.timeout(5):
            while True:
                task = (await self.client.get(f"/tasks/{task_id}")).json()
                if task["status"] in statuses:
                    return task
                if task["status"] in {"failed", "interrupted", "budget_exhausted"}:
                    self.fail(f"Unexpected terminal status: {task['status']}")
                await asyncio.sleep(0.002)

    async def start(self):
        created = await self.client.post(
            "/tasks", json={"request": "杭州日系人像，驾车40分钟"}
        )
        task = await self.wait_status(created.json()["id"], "awaiting_confirmation")
        response = await self.client.post(
            f"/tasks/{task['id']}/confirm",
            json={"revision": task["revision"], "accepted": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.search_calls, [])
        self.geo_provider.assert_not_awaited()
        response = await self.client.post(
            f"/tasks/{task['id']}/start", json={"revision": response.json()["revision"]}
        )
        self.assertEqual(response.status_code, 202, response.text)
        return task["id"]

    async def result(self, task_id):
        response = await self.client.get(f"/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    async def test_real_graph_http_confirmation_routes_budget_and_final_submission(
        self,
    ):
        task_id = await self.start()
        task = await self.wait_status(task_id, "awaiting_origin")
        self.assertEqual(self.model.calls, 1)  # Extraction only until human selects.
        self.assertEqual(self.search_calls, [])
        choice = task["origin_options"][0]["evidence_id"]
        response = await self.client.post(
            f"/tasks/{task_id}/origin",
            json={
                "revision": task["revision"],
                "evidence_id": choice,
            },
        )
        self.assertEqual(response.status_code, 202)
        await self.wait_status(task_id, "completed")
        result = await self.result(task_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["metrics"]["model_attempts"], self.model.calls)
        self.assertLessEqual(self.model.calls, 12)
        self.assertEqual(result["metrics"]["map_attempts"], 5)
        self.assertEqual(result["metrics"]["budget_rejections"], 0)
        self.assertEqual(len(self.search_calls), 1)
        self.assertEqual(len(self.route_calls), 3)
        plan = result["recommendation"]
        self.assertEqual(plan["confirmed_departure"]["evidence_id"], choice)
        self.assertEqual(
            [item["travel_time"]["status"] for item in plan["candidates"]],
            ["meets", "not_met", "not_met"],
        )
        self.assertIn("55 分钟 / 上限 40 分钟", result["final_text"])
        saved = self.app.state.service.store.get(task_id)
        self.assertEqual(saved.result.model_dump(mode="json"), result)

    async def test_extraction_usage_can_exhaust_shared_budget_before_map_work(self):
        self.runtime.limits = ExecutionLimits(model_calls=1)
        task_id = await self.start()
        await self.wait_status(task_id, "budget_exhausted")
        result = await self.result(task_id)
        self.assertEqual(result["metrics"]["model_attempts"], 1)
        self.assertEqual(result["metrics"]["map_attempts"], 0)
        self.geo_provider.assert_not_awaited()
        self.assertEqual(self.search_calls, [])

    async def test_partial_evidence_survives_failure_budget_stop_and_cancellation(self):
        for ending in ["failed", "budget_exhausted", "cancelled", "incomplete"]:
            with self.subTest(ending=ending):
                started = asyncio.Event()
                stopped = asyncio.Event()

                class PartialGraph:
                    async def astream(self, *args, **kwargs):
                        compact_result(
                            {"pois": [POI]}, "maps_text_search", current_run()
                        )
                        yield {
                            "type": "updates",
                            "data": {
                                "model": {
                                    "messages": [
                                        AIMessage(
                                            content="unsafe-freeform-provider-secret"
                                        )
                                    ]
                                }
                            },
                        }
                        started.set()
                        try:
                            if ending == "failed":
                                raise ValueError("unsafe-freeform-provider-secret")
                            if ending == "budget_exhausted":
                                raise ExecutionBudgetExceeded("model_calls")
                            if ending == "cancelled":
                                await asyncio.Event().wait()
                        finally:
                            stopped.set()

                with patch.object(
                    self.runtime,
                    "_planner",
                    AsyncMock(return_value=(PartialGraph(), None)),
                ):
                    task_id = await self.start()
                    await asyncio.wait_for(started.wait(), 2)
                    if ending == "cancelled":
                        task = (await self.client.get(f"/tasks/{task_id}")).json()
                        await self.client.post(
                            f"/tasks/{task_id}/cancel",
                            json={"revision": task["revision"]},
                        )
                    await self.wait_status(task_id, ending)
                    await asyncio.wait_for(stopped.wait(), 2)
                    jobs = list(self.app.state.service.jobs.values())
                    if jobs:
                        await asyncio.gather(*jobs)
                    result = await self.result(task_id)
                self.assertEqual(result["status"], ending)
                self.assertEqual(
                    result["recommendation"]["candidates"][0]["facts"]["name"],
                    POI["name"],
                )
                self.assertIn(POI["name"], result["final_text"])
                self.assertNotIn("unsafe-freeform", json.dumps(result))
        self.assertNotIn(b"unsafe-freeform", (self.path / "tasks.sqlite3").read_bytes())

    async def test_default_app_startup_health_and_docs_do_not_construct_providers(self):
        with (
            patch("scene_pilot.api.runtime.create_qwen_model") as model,
            patch(
                "scene_pilot.api.runtime.load_amap_tools", new_callable=AsyncMock
            ) as maps,
        ):
            app = create_app(self.path / "startup.sqlite3")
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                ) as client,
            ):
                self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertEqual((await client.get("/openapi.json")).status_code, 200)
            model.assert_not_called()
            maps.assert_not_awaited()

    async def test_extraction_configuration_failure_can_be_manually_corrected(self):
        self.runtime._model = None
        with patch(
            "scene_pilot.api.runtime.create_qwen_model",
            side_effect=ValueError("private-key"),
        ):
            response = await self.client.post("/tasks", json={"request": "杭州写真"})
            task = await self.wait_status(
                response.json()["id"], "awaiting_confirmation"
            )
        self.assertEqual(task["error_code"], "model_configuration_required")
        self.assertEqual(
            self.app.state.service.store.get(task["id"]).usage.model_attempts, 0
        )
        response = await self.client.patch(
            f"/tasks/{task['id']}/brief",
            json={
                "revision": task["revision"],
                "brief": {"city": "杭州", "photography_style": "日系"},
            },
        )
        response = await self.client.post(
            f"/tasks/{task['id']}/confirm",
            json={
                "revision": response.json()["revision"],
                "accepted": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertNotIn(b"private-key", (self.path / "tasks.sqlite3").read_bytes())
