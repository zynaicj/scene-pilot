"""Drive the actual LangGraph/Deep Agents runtime with a scripted offline model."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_amap_policy import POI, wrapped_tool

from scene_pilot.agent import create_planning_graph
from scene_pilot.agents.budgets import RunBudgetMiddleware
from scene_pilot.core.execution import (
    ExecutionBudgetExceeded,
    ExecutionLimits,
    current_run,
    execution_scope,
)
from scene_pilot.observability.streaming import StreamObserver


class ScriptedModel(BaseChatModel):
    script: Any = Field(exclude=True)
    bound_toolsets: list = Field(default_factory=list, exclude=True)
    calls: int = 0

    @property
    def _llm_type(self):
        return "offline-script"

    def bind_tools(self, tools, **kwargs):
        self.bound_toolsets.append(
            {
                tool.name if hasattr(tool, "name") else tool["function"]["name"]
                for tool in tools
            }
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        message = self.script(messages)
        message.usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, identity):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": identity, "type": "tool_call"}],
    )


class AgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_graph_delegation_evidence_and_output_boundary(self):
        async def search(**kwargs):
            return {"pois": [POI]}

        def script(messages):
            system = messages[0].content
            tool_messages = [message for message in messages if message.type == "tool"]
            if "你是摄影场景调研专员" in system:
                if not tool_messages:
                    return call("maps_text_search", {"keywords": "杭州公园"}, "map1")
                return AIMessage(content=tool_messages[-1].content)
            if not tool_messages:
                return call(
                    "task",
                    {
                        "subagent_type": "scene-agent",
                        "description": "杭州日系人像，检索公园，返回 evidence_id。",
                    },
                    "task1",
                )
            if tool_messages[-1].name == "task":
                data = json.loads(tool_messages[-1].content)
                return call(
                    "finalize_recommendation",
                    {
                        "user_statements": [{"quote": "淡妆"}],
                        "candidates": [
                            {
                                "evidence_id": data["pois"][0]["evidence_id"],
                                "advice": [{"text": "建议尝试柔光"}],
                            }
                        ],
                    },
                    "final1",
                )
            return AIMessage(content="虚构内容：门票已核实免费，丝绸衣服，已看现场照片")

        model = ScriptedModel(script=script)
        graph = create_planning_graph(model, [wrapped_tool(search)], [])
        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(Path(tmp), emit=lambda _: None).arun(
                graph, "杭州日系人像，淡妆"
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.metrics["model_attempts"], 4)
            self.assertEqual(result.metrics["map_attempts"], 1)
            self.assertEqual(result.model_calls, 4)
            self.assertEqual(result.input_tokens, 400)
            self.assertEqual(len(result.recommendation["candidates"]), 1)
            self.assertIn("测试公园", result.final_text)
            self.assertNotIn("已核实免费", result.final_text)
            self.assertNotIn("丝绸", result.final_text)
            self.assertNotIn("测试公园", result.trace_path.read_text(encoding="utf-8"))
        for tools in model.bound_toolsets:
            self.assertFalse(
                tools & {"execute", "read_file", "write_file", "ls", "glob", "grep"}
            )
        self.assertTrue(
            any(
                "task" in tools and "write_todos" in tools
                for tools in model.bound_toolsets
            )
        )

    async def test_global_model_budget_stops_real_loop_and_resets(self):
        def script(messages):
            return call(
                "write_todos",
                {"todos": [{"content": "调研", "status": "in_progress"}]},
                f"todo{len(messages)}",
            )

        model = ScriptedModel(script=script)
        graph = create_planning_graph(model, [], [])
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                result = await StreamObserver(
                    Path(tmp),
                    emit=lambda _: None,
                    limits=ExecutionLimits(model_calls=2),
                ).arun(graph, "杭州日系人像")
                self.assertEqual(result.status, "budget_exhausted")
                self.assertEqual(result.metrics["model_attempts"], 2)
                self.assertIn("没有取得", result.final_text)
                self.assertIn(
                    "budget_stop", result.trace_path.read_text(encoding="utf-8")
                )
        self.assertEqual(model.calls, 4)

    async def test_deadline_cancels_underlying_stream(self):
        cancelled = asyncio.Event()

        class SlowAgent:
            async def astream(self, *args, **kwargs):
                try:
                    await asyncio.sleep(10)
                    yield {}
                finally:
                    cancelled.set()

        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(
                Path(tmp),
                emit=lambda _: None,
                limits=ExecutionLimits(timeout_seconds=0.01),
            ).arun(SlowAgent(), "杭州")
            self.assertTrue(cancelled.is_set())
            self.assertEqual(result.status, "budget_exhausted")

    async def test_parallel_reservation_is_atomic(self):
        middleware = RunBudgetMiddleware()

        async def handler(request):
            await asyncio.sleep(0)
            from langchain.agents.middleware.types import ModelResponse

            return ModelResponse(result=[AIMessage(content="done")])

        with execution_scope("", ExecutionLimits(model_calls=1)) as context:
            results = await asyncio.gather(
                middleware.awrap_model_call(None, handler),
                middleware.awrap_model_call(None, handler),
                return_exceptions=True,
            )
            self.assertEqual(
                sum(isinstance(value, ExecutionBudgetExceeded) for value in results), 1
            )
            self.assertEqual(context.metrics["model_attempts"], 1)
            self.assertEqual(context.metrics["usage_missing"], 1)

    async def test_observed_token_budget(self):
        def script(messages):
            return call("write_todos", {"todos": []}, "todo")

        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(
                Path(tmp),
                emit=lambda _: None,
                limits=ExecutionLimits(observed_tokens=100),
            ).arun(create_planning_graph(ScriptedModel(script=script), [], []), "杭州")
            self.assertEqual(result.metrics["model_attempts"], 1)
            self.assertEqual(result.status, "budget_exhausted")

    async def test_context_cleanup(self):
        with self.assertRaises(ExecutionBudgetExceeded):
            current_run()
        with execution_scope("private"):
            self.assertEqual(current_run().request, "private")
        with self.assertRaises(ExecutionBudgetExceeded):
            current_run()

    async def test_long_input_stops_before_model(self):
        model = ScriptedModel(script=lambda _: AIMessage(content="unexpected"))
        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(Path(tmp), emit=lambda _: None).arun(
                create_planning_graph(model, [], []),
                "x" * 12001,
            )
            self.assertEqual(model.calls, 0)
            self.assertEqual(result.status, "budget_exhausted")

    async def test_fabricated_filesystem_tool_is_denied(self):
        def script(messages):
            results = [message for message in messages if message.type == "tool"]
            if not results:
                return call(
                    "write_file",
                    {"file_path": "/private.txt", "content": "private"},
                    "denied",
                )
            self.assertIn("tool_not_allowed", results[-1].content)
            return AIMessage(content="done")

        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(Path(tmp), emit=lambda _: None).arun(
                create_planning_graph(ScriptedModel(script=script), [], []),
                "杭州",
            )
            self.assertEqual(result.tool_failures, 1)

    async def test_unverified_free_text_never_becomes_final_recommendation(self):
        model = ScriptedModel(script=lambda _: AIMessage(content="编造公园，免费开放"))
        with tempfile.TemporaryDirectory() as tmp:
            result = await StreamObserver(Path(tmp), emit=lambda _: None).arun(
                create_planning_graph(model, [], []),
                "杭州",
            )
            self.assertEqual(result.status, "incomplete")
            self.assertNotIn("编造公园", result.final_text)
            self.assertEqual(result.recommendation["candidates"], [])
