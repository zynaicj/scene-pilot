"""仅使用标准库测试流式事件解析，不调用 Qwen 或高德。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from scene_pilot.observability.streaming import StreamObserver, _tool_status


class FakeAgent:
    def stream(self, *args, **kwargs):
        assert kwargs["subgraphs"] is True
        assert kwargs["version"] == "v2"
        ai_task = SimpleNamespace(type="ai", id="ai1", tool_calls=[{"id": "call1", "name": "task", "args": {"subagent_type": "destination-agent", "description": "contains private text"}}], usage_metadata={"input_tokens": 30, "output_tokens": 10})
        tool_result = SimpleNamespace(type="tool", tool_call_id="call1", name="task", content="代理已返回", status="success")
        ai_final = SimpleNamespace(type="ai", id="ai2", tool_calls=[], content="候选地点调研完成", usage_metadata={"input_tokens": 20, "output_tokens": 15})
        yield {"type": "updates", "ns": (), "data": {"model": {"messages": [ai_task]}}}
        yield {"type": "updates", "ns": ("tools:subagent-id",), "data": {"model": {"messages": [SimpleNamespace(type="ai", id="ai3", tool_calls=[{"id": "call2", "name": "search_destinations", "args": {"keyword": "private-key"}}], usage_metadata={"input_tokens": 7, "output_tokens": 3})]}}}
        yield {"type": "updates", "ns": ("tools:subagent-id",), "data": {"tools": {"messages": [SimpleNamespace(type="tool", tool_call_id="call2", name="search_destinations", content="{'status': 'error', 'message': 'sensitive private-key'}")]}}}
        yield {"type": "updates", "ns": (), "data": {"tools": {"messages": [tool_result]}}}
        yield {"type": "updates", "ns": (), "data": {"model": {"messages": [ai_final]}}}


class StreamingTests(unittest.TestCase):
    def test_stream_records_safe_metrics(self):
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = StreamObserver(Path(tmp), emit=lines.append).run(FakeAgent(), "contains secret")
            data = summary.trace_path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in data.splitlines()]
            self.assertEqual(summary.final_text, "候选地点调研完成")
            self.assertEqual(summary.model_calls, 3)
            self.assertEqual(summary.tool_calls, 2)
            self.assertEqual(summary.tool_failures, 1)
            self.assertEqual(summary.input_tokens, 57)
            self.assertEqual(summary.output_tokens, 28)
            self.assertIn("subagent_delegated", [event["event"] for event in events])
            self.assertNotIn("private-key", data)
            self.assertNotIn("contains secret", data)
            self.assertNotIn("sensitive", data)
            self.assertTrue(any("destination-agent" in line for line in lines))

    def test_tool_status(self):
        self.assertEqual(_tool_status(SimpleNamespace(content="{'status': 'error'}", status="success")), "error")
        self.assertEqual(_tool_status(SimpleNamespace(content='{"status":"success"}', status="success")), "success")
        self.assertEqual(_tool_status(SimpleNamespace(content="", status="error")), "error")
        self.assertEqual(_tool_status(SimpleNamespace(content="普通工具回复", status="success")), "executed")

    def test_exception_records_type_only(self):
        class BrokenAgent:
            def stream(self, *args, **kwargs):
                raise RuntimeError("private api key")
        with tempfile.TemporaryDirectory() as tmp:
            observer = StreamObserver(Path(tmp), emit=lambda x: None)
            with self.assertRaises(RuntimeError):
                observer.run(BrokenAgent(), "hello")
            data = observer.trace_path.read_text(encoding="utf-8")
            self.assertNotIn("private api key", data)
            self.assertIn('"error_type": "RuntimeError"', data)


if __name__ == "__main__":
    unittest.main()
