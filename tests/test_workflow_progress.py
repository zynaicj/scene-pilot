"""Real graph regressions for planning/search loops, without external services."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config
from pydantic import Field

from scene_pilot.agent import create_planning_graph
from scene_pilot.agents.budgets import DomainToolsMiddleware
from scene_pilot.core.execution import ExecutionLimits, execution_scope
from scene_pilot.core.recommendation import MapEvidence
from scene_pilot.observability.streaming import StreamObserver
from scene_pilot.integrations.amap_results import compact_result
from test_agent_execution import ScriptedModel, call
from test_amap_policy import wrapped_tool
from test_environment import NOW, weather_payload, weather_tool


class StepModel(ScriptedModel):
    supervisor_steps: list[int] = Field(default_factory=list)
    active_tools: set[str] = Field(default_factory=set)

    def bind_tools(self, tools, **kwargs):
        super().bind_tools(tools, **kwargs)
        return self.bind(active_tools=self.bound_toolsets[-1])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.active_tools = kwargs.pop("active_tools", set())
        if "你是 ScenePilot" in messages[0].content:
            self.supervisor_steps.append(get_config()["metadata"]["langgraph_step"])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class WorkflowProgressTests(unittest.IsolatedAsyncioTestCase):
    async def run_planning(self, *, enough_on_first=True, with_routes=False):
        searches = []
        weather_calls = []

        async def search(**kwargs):
            searches.append(kwargs)
            count = 3 if enough_on_first else len(searches)
            return {
                "pois": [
                    {
                        "id": f"B{i}",
                        "name": f"测试公园{i}",
                        **({"location": f"120.{i},30"} if with_routes else {}),
                    }
                    for i in range(count)
                ]
            }

        async def driving(**kwargs):
            return {"paths": [{"distance": "1000", "duration": "600"}]}

        async def forecast(city):
            weather_calls.append(city)
            return weather_payload()

        def script(messages):
            system = messages[0].content
            results = [m for m in messages if m.type == "tool"]
            available = model.active_tools
            if "你是人像约拍摄影风格分析专员" in system:
                return AIMessage(content="建议搜索公园")
            if "你是摄影场景调研专员" in system:
                # This model wants to search again whenever the backend still
                # offers search. Prompt-only changes cannot pass this regression.
                if "maps_text_search" in available and len(results) < 2:
                    return call(
                        "maps_text_search",
                        {"keywords": str(len(results)), "city": "杭州市"},
                        f"search{len(results)}",
                    )
                if "maps_around_search" in available and len(results) == 2:
                    return call("maps_around_search", {"keywords": "公园"}, "extra")
                self.assertNotIn("maps_text_search", available)
                self.assertNotIn("maps_around_search", available)
                return AIMessage(content=results[-1].content)
            if "你是摄影交通路线专员" in system:
                if not results:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "maps_direction_driving",
                                "args": {
                                    "origin": "119,30",
                                    "destination": f"120.{i},30",
                                },
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
            task_results = [item for item in results if item.name == "task"]
            if "prepare_photography_brief" not in names:
                brief = call(
                    "prepare_photography_brief",
                    {
                        "city": "杭州",
                        "photography_style": "日系",
                        "shooting_date": "2026-10-17",
                        **(
                            {"departure": "119,30", "transport_mode": "驾车"}
                            if with_routes
                            else {}
                        ),
                    },
                    "brief",
                )
                if with_routes:
                    brief.tool_calls += call(
                        "write_todos", {"todos": []}, "initial_plan"
                    ).tool_calls
                return brief
            if "write_todos" not in names or (
                task_results
                and results[-1].name == "task"
                and "write_todos" in available
            ):
                return call("write_todos", {"todos": []}, f"todo{len(results)}")
            if len(task_results) < 2:
                return call(
                    "task",
                    {
                        "subagent_type": "style-agent"
                        if not task_results
                        else "scene-agent",
                        "description": "杭州日系公园，返回证据编号",
                    },
                    f"task{len(task_results)}",
                )
            pois = json.loads(task_results[1].content)["pois"]
            if with_routes and len(task_results) == 2:
                return call(
                    "task",
                    {
                        "subagent_type": "route-agent",
                        "description": "从119,30驾车到已知候选坐标，返回路线证据",
                    },
                    "route_task",
                )
            if "inspect_shooting_conditions" not in names:
                return call(
                    "inspect_shooting_conditions",
                    {"evidence_ids": [item["evidence_id"] for item in pois]},
                    "conditions",
                )
            if "finalize_recommendation" not in names:
                drafts = [{"evidence_id": item["evidence_id"]} for item in pois]
                if with_routes:
                    routes = json.loads(task_results[2].content)
                    for draft, route in zip(drafts, routes, strict=True):
                        draft["route_evidence_id"] = route["paths"][0][
                            "route_evidence_id"
                        ]
                return call(
                    "finalize_recommendation",
                    {"user_statements": [], "candidates": drafts},
                    "final",
                )
            self.fail("A validated final submission must not spend another model call")

        model = StepModel(script=script)
        graph = create_planning_graph(
            model,
            [wrapped_tool(search), wrapped_tool(search, "maps_around_search")],
            [wrapped_tool(driving, "maps_direction_driving")],
            [weather_tool(forecast)],
        )
        self.assertNotIn(
            "SummarizationMiddleware.before_model", graph.get_graph().nodes
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scene_pilot.tools.environment_tools.datetime") as clock,
        ):
            clock.now.return_value = NOW
            result = await StreamObserver(Path(directory), emit=lambda _: None).arun(
                graph, "杭州日系 2026-10-17 从119,30出发"
            )
            events = [
                json.loads(line)
                for line in result.trace_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.recommendation["status"], "completed")
        self.assertEqual(ExecutionLimits().recursion_limit, 40)
        self.assertLessEqual(result.metrics["model_attempts"], 12)
        self.assertEqual(model.supervisor_steps, [3, 9, 15, 21, 27, 33])
        self.assertEqual(
            sum(
                e.get("tool") == "write_todos" and e["event"] == "tool_requested"
                for e in events
            ),
            1,
        )
        self.assertEqual(weather_calls, ["杭州市"])
        self.assertEqual(len(searches), 1 if enough_on_first else 2)
        self.assertEqual(result.metrics["budget_rejections"], 0)
        return result

    async def test_three_candidates_missing_coordinates_still_finalize(self):
        result = await self.run_planning()
        self.assertEqual(result.metrics["model_attempts"], 9)
        self.assertEqual(len(result.recommendation["candidates"]), 3)
        for item in result.recommendation["candidates"]:
            self.assertIsNone(item["facts"]["location"])
            self.assertIsNone(item["driving_route"])
            self.assertEqual(item["conditions"]["solar"]["status"], "unknown")
            self.assertEqual(item["conditions"]["weather"]["status"], "reference")

    async def test_search_budget_exhausted_with_two_candidates_still_finalize(self):
        result = await self.run_planning(enough_on_first=False)
        self.assertEqual(len(result.recommendation["candidates"]), 2)
        self.assertEqual(result.metrics["search_attempts"], 2)

    async def test_routes_and_weather_fit_unchanged_budgets(self):
        result = await self.run_planning(with_routes=True)
        self.assertEqual(result.metrics["model_attempts"], 11)
        self.assertEqual(result.metrics["map_attempts"], 5)
        self.assertTrue(
            all(item["driving_route"] for item in result.recommendation["candidates"])
        )

    async def test_hidden_search_is_also_rejected_at_execution(self):
        middleware = DomainToolsMiddleware(
            {"maps_text_search", "maps_around_search"}, "scene", phase="scene"
        )
        with execution_scope("") as context:
            for index in range(3):
                context.evidence[str(index)] = MapEvidence(
                    evidence_id=str(index),
                    poi_id=str(index),
                    name="测试",
                    tool="maps_text_search",
                    retrieved_at=NOW,
                )
            request = SimpleNamespace(
                state={"messages": []},
                tool_call={"name": "maps_around_search", "id": "extra"},
            )

            async def forbidden(_):
                self.fail("A hidden search must not reach its provider")

            denied = await middleware.awrap_tool_call(request, forbidden)
            self.assertEqual(denied.status, "error")
            self.assertEqual(context.metrics["map_attempts"], 0)

    def test_duplicate_evidence_versions_do_not_count_as_three_places(self):
        middleware = DomainToolsMiddleware(
            {"maps_text_search", "maps_around_search", "maps_search_detail"},
            "scene",
            phase="scene",
        )
        with execution_scope("") as context:
            for index in range(3):
                context.evidence[str(index)] = MapEvidence(
                    evidence_id=str(index),
                    poi_id="same-poi",
                    name="测试",
                    tool="maps_text_search",
                    retrieved_at=NOW,
                )
            self.assertIn("maps_text_search", middleware._available([]))
            context.metrics["search_attempts"] = 2
            self.assertEqual(middleware._available([]), {"maps_search_detail"})

    async def test_repeated_todo_execution_is_rejected(self):
        middleware = DomainToolsMiddleware({"write_todos", "task"}, "supervisor")
        request = SimpleNamespace(
            state={
                "messages": [
                    ToolMessage(
                        content="saved", name="write_todos", tool_call_id="first"
                    )
                ]
            },
            tool_call={"name": "write_todos", "id": "repeat"},
        )

        async def forbidden(_):
            self.fail("A repeated plan update must not run")

        denied = await middleware.awrap_tool_call(request, forbidden)
        self.assertEqual(denied.status, "error")

    def test_missing_coordinates_stay_unknown_until_poi_detail_provides_them(self):
        with execution_scope("") as context:
            raw = {"id": "B1", "name": "测试公园"}
            first = compact_result({"pois": [raw]}, "maps_text_search", context)[
                "pois"
            ][0]
            self.assertIsNone(first["location"])
            detail = compact_result(
                {**raw, "location": "120.1,30.2"},
                "maps_search_detail",
                context,
            )["pois"][0]
            self.assertEqual(detail["poi_id"], first["poi_id"])
            self.assertEqual(detail["location"], "120.1,30.2")
            self.assertNotEqual(detail["evidence_id"], first["evidence_id"])
            self.assertIsNone(context.evidence[first["evidence_id"]].location)

    async def test_invalid_final_can_be_corrected_and_then_stops(self):
        async def search(**kwargs):
            return {"pois": [{"id": "B1", "name": "测试"}]}

        def script(messages):
            results = [item for item in messages if item.type == "tool"]
            if "你是摄影场景调研专员" in messages[0].content:
                return (
                    AIMessage(content=results[-1].content)
                    if results
                    else call("maps_text_search", {}, "s")
                )
            if not results:
                return call(
                    "task",
                    {"subagent_type": "scene-agent", "description": "杭州"},
                    "task",
                )
            if results[-1].name == "task":
                return call(
                    "finalize_recommendation",
                    {
                        "user_statements": [],
                        "candidates": [{"evidence_id": "invented"}],
                    },
                    "bad",
                )
            self.assertIn("evidence_validation_failed", results[-1].content)
            poi = json.loads(results[0].content)["pois"][0]
            return call(
                "finalize_recommendation",
                {
                    "user_statements": [],
                    "candidates": [{"evidence_id": poi["evidence_id"]}],
                },
                "corrected",
            )

        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(Path(directory), emit=lambda _: None).arun(
                create_planning_graph(
                    ScriptedModel(script=script), [wrapped_tool(search)], []
                ),
                "杭州",
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics["model_attempts"], 5)
        self.assertEqual(
            result.recommendation["candidates"][0]["facts"]["poi_id"], "B1"
        )
