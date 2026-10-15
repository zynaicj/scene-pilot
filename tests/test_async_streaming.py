"""异步流式统计不访问任何外部网络。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from group_pilot.observability.streaming import StreamObserver


class FakeAsyncAgent:
    async def astream(self, *args, **kwargs):
        assert kwargs["version"] == "v2"
        assert kwargs["subgraphs"] is True
        yield {
            "type": "updates",
            "ns": (),
            "data": {"model": {"messages": [SimpleNamespace(
                type="ai", id="ai-final", content="摄影场景建议", tool_calls=[],
                usage_metadata={"input_tokens": 13, "output_tokens": 7},
            )]}},
        }


class AsyncStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = await StreamObserver(Path(tmp), emit=lambda _: None).arun(FakeAsyncAgent(), "私密拍摄需求")
            self.assertEqual(summary.final_text, "摄影场景建议")
            self.assertEqual(summary.model_calls, 1)
            self.assertEqual(summary.input_tokens, 13)
            self.assertEqual(summary.output_tokens, 7)
            content = summary.trace_path.read_text(encoding="utf-8")
            self.assertIn("run_completed", content)
            self.assertNotIn("私密拍摄需求", content)


if __name__ == "__main__":
    unittest.main()
