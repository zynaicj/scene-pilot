"""Public phases never reflect provider text; notification failure is non-fatal."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from scene_pilot.core.progress import TaskProgress, progress_from_event
from scene_pilot.observability.streaming import StreamObserver
from test_agent_execution import ScriptedModel, call
from test_amap_policy import POI, wrapped_tool
from scene_pilot.agent import create_planning_graph


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_only_allowlisted_tools_and_roles_become_progress(self):
        self.assertIsNone(
            progress_from_event("tool_requested", {"tool": "private-key"})
        )
        self.assertIsNone(
            progress_from_event("subagent_delegated", {"subagent": "private-address"})
        )
        self.assertIsNone(
            progress_from_event("run_failed", {"error_type": "private-url"})
        )
        result = progress_from_event(
            "tool_requested",
            {
                "tool": "inspect_shooting_conditions",
                "args": "private-address",
                "content": "private-provider-output",
            },
        )
        self.assertEqual(result.phase, "conditions")
        self.assertNotIn("private", result.model_dump_json())
        with self.assertRaises(ValueError):
            TaskProgress(phase="private")
        with self.assertRaises(ValueError):
            TaskProgress(phase="searching", message="private")

    async def test_progress_sink_failure_does_not_abort_graph_or_change_budget(self):
        async def search(**kwargs):
            return {"pois": [POI]}

        def script(messages):
            results = [item for item in messages if item.type == "tool"]
            if "你是摄影场景调研专员" in messages[0].content:
                if not results:
                    return call("maps_text_search", {"keywords": "公园"}, "search")
                return AIMessage(content=results[-1].content)
            if not results:
                return call(
                    "task",
                    {"subagent_type": "scene-agent", "description": "公园"},
                    "task",
                )
            return AIMessage(content="unsafe-freeform")

        model = ScriptedModel(script=script)
        graph = create_planning_graph(model, [wrapped_tool(search)], [])
        callback = Mock(side_effect=RuntimeError("private-sink-error"))
        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(
                Path(directory), emit=lambda _: None, on_progress=callback
            ).arun(graph, "杭州公园")
            trace = result.trace_path.read_text(encoding="utf-8")
        self.assertTrue(callback.called)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.metrics["map_attempts"], 1)
        self.assertEqual(result.metrics["model_attempts"], 4)
        self.assertNotIn("unsafe-freeform", result.final_text)
        self.assertNotIn("private-sink-error", trace)
