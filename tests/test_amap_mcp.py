"""高德 MCP 工具白名单、并发/重试与错误安全性测试（完全离线）。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel

from scene_pilot.core.execution import execution_scope
from scene_pilot.integrations.amap_mcp import (
    AmapMCPError,
    _transient_transport_error,
    build_amap_mcp_client,
    load_amap_tools,
    protect_amap_tools,
    select_amap_tools,
)


class SearchInput(BaseModel):
    keywords: str


def mock_map_tool(name: str, coroutine):
    return StructuredTool.from_function(
        name=name,
        description="根据关键字查询高德地图",
        coroutine=coroutine,
        args_schema=SearchInput,
    )


class BrokenResourceError(Exception):
    """模拟 anyio 连接断开。"""


class AmapMCPTests(unittest.TestCase):
    def test_tool_allowlist_and_roles(self):
        tools = [
            SimpleNamespace(name="maps_text_search"),
            SimpleNamespace(name="maps_direction_driving"),
            SimpleNamespace(name="maps_search_detail"),
            SimpleNamespace(name="maps_geo"),
            SimpleNamespace(name="maps_weather"),
            SimpleNamespace(name="execute"),
        ]
        scene, route = select_amap_tools(tools)
        self.assertEqual(
            [t.name for t in scene], ["maps_text_search", "maps_search_detail"]
        )
        self.assertEqual(
            [t.name for t in route], ["maps_direction_driving", "maps_geo"]
        )

    def test_missing_core_tools(self):
        with self.assertRaises(AmapMCPError):
            select_amap_tools([SimpleNamespace(name="maps_geo")])

    def test_missing_key(self):
        with self.assertRaises(AmapMCPError):
            build_amap_mcp_client(" ")

    def test_connection_errors_only(self):
        self.assertTrue(_transient_transport_error(BrokenResourceError()))
        self.assertTrue(_transient_transport_error(ConnectionResetError()))
        self.assertTrue(_transient_transport_error(TimeoutError()))
        self.assertFalse(_transient_transport_error(ValueError("HTTP 403")))

    def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            protect_amap_tools([], max_concurrency=0)


class AmapMCPAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_handled_error_does_not_leak_content(self):
        async def lookup(keywords: str):
            raise ToolException("https://mcp.amap.com/?key=private")

        source = mock_map_tool("maps_text_search", lookup)
        source.handle_tool_error = True
        result = await protect_amap_tools([source])[0].ainvoke({"keywords": "公园"})
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", str(result))

    async def test_structured_artifact_is_preserved(self):
        async def lookup(keywords: str):
            return "display", {"structured_content": {"pois": [{"name": keywords}]}}

        source = mock_map_tool("maps_text_search", lookup)
        source.response_format = "content_and_artifact"
        result = await protect_amap_tools([source])[0].ainvoke({"keywords": "公园"})
        self.assertEqual(result["pois"][0]["name"], "公园")

    async def test_cancellation_releases_semaphore(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def lookup(keywords: str):
            if keywords == "wait":
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            return keywords

        guarded = protect_amap_tools([mock_map_tool("maps_text_search", lookup)])[0]
        pending = asyncio.create_task(guarded.ainvoke({"keywords": "wait"}))
        await started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(cancelled.is_set())
        self.assertEqual(await guarded.ainvoke({"keywords": "next"}), "next")

    async def test_load_tools_without_network(self):
        async def lookup(keywords: str):
            return {"pois": [{"id": "B001", "name": keywords}]}

        tools = [
            mock_map_tool("maps_text_search", lookup),
            mock_map_tool("maps_direction_driving", lookup),
        ]
        fake = SimpleNamespace(get_tools=AsyncMock(return_value=tools))
        with patch(
            "scene_pilot.integrations.amap_mcp.build_amap_mcp_client", return_value=fake
        ):
            scene, route = await load_amap_tools("test-key")
            self.assertEqual(len(scene), 1)
            self.assertEqual(len(route), 1)
            with execution_scope("杭州"):
                result = await scene[0].ainvoke({"keywords": "杭州公园"})
            self.assertIn("杭州公园", str(result))
            fake.get_tools.assert_awaited_once_with(server_name="amap")

    async def test_redact_connection_errors(self):
        fake = SimpleNamespace(
            get_tools=AsyncMock(side_effect=RuntimeError("secret-api-key in url"))
        )
        with patch(
            "scene_pilot.integrations.amap_mcp.build_amap_mcp_client", return_value=fake
        ):
            with self.assertRaises(AmapMCPError) as caught:
                await load_amap_tools("secret-api-key")
            self.assertNotIn("secret-api-key", str(caught.exception))

    async def test_transport_disconnect_recovers(self):
        calls = 0

        async def lookup(keywords: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BrokenResourceError("secret-key")
            return f"找到 {keywords}"

        source = mock_map_tool("maps_text_search", lookup)
        guarded = protect_amap_tools([source], retry_delay_seconds=0)[0]
        result = await guarded.ainvoke({"keywords": "西湖"})
        self.assertIn("西湖", str(result))
        self.assertEqual(calls, 2)

    async def test_exhausted_retries_report_safe_error(self):
        calls = 0

        async def lookup(keywords: str):
            nonlocal calls
            calls += 1
            raise BrokenResourceError("AMAP_WEB_KEY=private")

        source = mock_map_tool("maps_text_search", lookup)
        guarded = protect_amap_tools([source], retry_delay_seconds=0)[0]
        result = await guarded.ainvoke({"keywords": "西湖"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "transport_unavailable")
        self.assertNotIn("private", str(result))
        self.assertEqual(calls, 2)

    async def test_provider_error_is_not_retried(self):
        calls = 0

        async def lookup(keywords: str):
            nonlocal calls
            calls += 1
            raise ValueError("HTTP 403 key=private")

        source = mock_map_tool("maps_text_search", lookup)
        guarded = protect_amap_tools([source], retry_delay_seconds=0)[0]
        result = await guarded.ainvoke({"keywords": "西湖"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "tool_execution_failed")
        self.assertNotIn("private", str(result))
        self.assertEqual(calls, 1)

    async def test_tools_share_semaphore(self):
        active = 0
        peak = 0

        async def lookup(keywords: str):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return keywords

        first = mock_map_tool("maps_text_search", lookup)
        second = mock_map_tool("maps_direction_driving", lookup)
        protected = protect_amap_tools([first, second], max_concurrency=1)
        result = await asyncio.gather(
            *[protected[i % 2].ainvoke({"keywords": str(i)}) for i in range(5)]
        )
        self.assertEqual(peak, 1)
        self.assertEqual(len(result), 5)

    async def test_request_timeout_is_reported(self):
        async def lookup(keywords: str):
            await asyncio.sleep(1)
            return keywords

        source = mock_map_tool("maps_text_search", lookup)
        guarded = protect_amap_tools(
            [source],
            timeout_seconds=0.001,
            max_retries=0,
        )[0]
        result = await guarded.ainvoke({"keywords": "杭州"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "transport_unavailable")


if __name__ == "__main__":
    unittest.main()
