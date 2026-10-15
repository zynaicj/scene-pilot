"""MCP 工具白名单与安全初始化测试，不连接高德服务器。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from scene_pilot.integrations.amap_mcp import (
    AmapMCPError, build_amap_mcp_client, load_amap_tools, select_amap_tools,
)


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
        self.assertEqual([t.name for t in scene], ["maps_text_search", "maps_search_detail"])
        self.assertEqual([t.name for t in route], ["maps_direction_driving", "maps_geo"])

    def test_missing_core_tools(self):
        with self.assertRaises(AmapMCPError):
            select_amap_tools([SimpleNamespace(name="maps_geo")])

    def test_missing_key(self):
        with self.assertRaises(AmapMCPError):
            build_amap_mcp_client(" ")


class AmapMCPAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_tools_without_network(self):
        fake = SimpleNamespace(get_tools=AsyncMock(return_value=[
            SimpleNamespace(name="maps_text_search"),
            SimpleNamespace(name="maps_direction_driving"),
        ]))
        with patch("scene_pilot.integrations.amap_mcp.build_amap_mcp_client", return_value=fake):
            scene, route = await load_amap_tools("test-key")
            self.assertEqual(len(scene), 1)
            self.assertEqual(len(route), 1)
            fake.get_tools.assert_awaited_once_with(server_name="amap")

    async def test_redact_connection_errors(self):
        fake = SimpleNamespace(get_tools=AsyncMock(side_effect=RuntimeError("secret-api-key in url")))
        with patch("scene_pilot.integrations.amap_mcp.build_amap_mcp_client", return_value=fake):
            with self.assertRaises(AmapMCPError) as caught:
                await load_amap_tools("secret-api-key")
            self.assertNotIn("secret-api-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
