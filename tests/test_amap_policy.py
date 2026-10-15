"""Cache, quotas, response schemas and privacy without any network access."""

import asyncio
import json
import unittest

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from scene_pilot.core.execution import ExecutionLimits, execution_scope
from scene_pilot.integrations.amap_mcp import protect_amap_tools
from scene_pilot.integrations.amap_policy import govern_amap_tools
from scene_pilot.integrations.amap_results import compact_result

POI = {"id": "B001", "name": "测试公园", "address": "测试路", "location": "120.1,30.2"}


class Arguments(BaseModel):
    keywords: str = ""
    id: str = ""
    origin: str = ""
    destination: str = ""


def wrapped_tool(fn, name="maps_text_search", **kwargs):
    source = StructuredTool.from_function(
        name=name,
        description="Offline fixture",
        coroutine=fn,
        args_schema=Arguments,
    )
    return govern_amap_tools(protect_amap_tools([source], **kwargs), "fake-secret")[0]


class MapPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_duplicate_search_and_poi_dedup(self):
        calls = 0

        async def search(**kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return [{"type": "text", "text": json.dumps({"pois": [POI, POI]})}]

        tool = wrapped_tool(search)
        with execution_scope("公园") as context:
            results = await asyncio.gather(
                *[tool.ainvoke({"keywords": "公园"}) for _ in range(5)]
            )
            self.assertEqual(calls, 1)
            self.assertEqual(context.metrics["cache_hits"], 4)
            self.assertEqual(len(context.evidence), 1)
            self.assertEqual(len(results[0]["pois"]), 1)
            results[0]["pois"].clear()
            self.assertEqual(len((await tool.ainvoke({"keywords": "公园"}))["pois"]), 1)

    async def test_detail_cache_by_poi_id_and_request_reset(self):
        calls = 0

        async def detail(**kwargs):
            nonlocal calls
            calls += 1
            return {"pois": [POI]}

        tool = wrapped_tool(detail, "maps_search_detail")
        for _ in range(2):
            with execution_scope("公园") as context:
                await tool.ainvoke({"id": "B001"})
                await tool.ainvoke({"id": "B001"})
                self.assertEqual(context.metrics["detail_attempts"], 1)
        self.assertEqual(calls, 2)

    async def test_ttl_expiry(self):
        async def search(**kwargs):
            return {"pois": [POI]}

        tool = wrapped_tool(search)
        with execution_scope("公园") as context:
            await tool.ainvoke({})
            entries = [key for key in context.cache if key != "lock"]
            timestamp, value = context.cache[entries[0]]
            context.cache[entries[0]] = (timestamp - 301, value)
            await tool.ainvoke({})
            self.assertEqual(context.metrics["map_attempts"], 2)

    async def test_search_quota_and_cache_hit_after_exhaustion(self):
        async def search(**kwargs):
            return {"pois": [POI]}

        tool = wrapped_tool(search)
        with execution_scope("公园") as context:
            await tool.ainvoke({"keywords": "one"})
            await tool.ainvoke({"keywords": "two"})
            blocked = await tool.ainvoke({"keywords": "three"})
            cached = await tool.ainvoke({"keywords": "one"})
            self.assertEqual(blocked["error_code"], "map_budget_exceeded")
            self.assertEqual(cached["status"], "success")
            self.assertEqual(context.metrics["map_attempts"], 2)

    async def test_retries_spend_total_budget(self):
        calls = 0

        async def search(**kwargs):
            nonlocal calls
            calls += 1
            raise ConnectionError("fake-secret")

        tool = wrapped_tool(search, retry_delay_seconds=0)
        with execution_scope("公园", ExecutionLimits(map_attempts=1)) as context:
            result = await tool.ainvoke({})
            self.assertEqual(result["error_code"], "map_budget_exceeded")
            self.assertEqual(calls, 1)
            self.assertEqual(context.metrics["map_attempts"], 1)

    async def test_failed_results_are_not_cached(self):
        async def search(**kwargs):
            return {"status": "0", "info": "fake-secret key=private"}

        tool = wrapped_tool(search)
        with execution_scope("公园") as context:
            first = await tool.ainvoke({})
            await tool.ainvoke({})
            self.assertEqual(context.metrics["cache_hits"], 0)
            self.assertNotIn("fake-secret", str(first))
            self.assertEqual(first["status"], "error")

    async def test_structured_error_code_cannot_leak_secret(self):
        async def search(**kwargs):
            return "ignored", {
                "structured_content": {
                    "status": "error",
                    "error_code": "https://host/?key=fake-secret",
                }
            }

        source = StructuredTool.from_function(
            name="maps_text_search",
            description="Fixture",
            coroutine=search,
            args_schema=Arguments,
            response_format="content_and_artifact",
        )
        tool = govern_amap_tools(protect_amap_tools([source]), "fake-secret")[0]
        with execution_scope("公园"):
            result = await tool.ainvoke({})
            self.assertEqual(result["error_code"], "invalid_tool_response")
            self.assertNotIn("fake-secret", str(result))

    async def test_malformed_and_long_results_fail_closed(self):
        for payload in [
            "fake-secret" * 30000,
            "not JSON",
            {"pois": None},
            [{"type": "image", "data": "private"}],
            {"pois": [{**POI, "name": "fake-secret"}]},
            {"pois": [{**POI, "location": "nan,30"}]},
        ]:

            async def search(payload=payload, **kwargs):
                return payload

            with execution_scope("公园"):
                result = await wrapped_tool(search).ainvoke({})
                self.assertEqual(result["status"], "error")
                self.assertNotIn("private", str(result))
                self.assertNotIn("fake-secret", str(result))

    async def test_compaction_preserves_full_facts_and_reports_omissions(self):
        payload = {
            "pois": [
                {
                    **POI,
                    "id": f"B{i}",
                    "photos": ["x" * 2000],
                    "biz_ext": {"cost": "0", "rating": "5.0"},
                }
                for i in range(10)
            ]
        }

        async def search(**kwargs):
            return payload

        with execution_scope("公园") as context:
            result = await wrapped_tool(search).ainvoke({})
            self.assertEqual(len(result["pois"]), 6)
            self.assertEqual(result["omitted_count"], 4)
            self.assertEqual(result["pois"][0]["address"], POI["address"])
            self.assertNotIn("biz_ext", str(result))
            self.assertLess(
                context.metrics["compact_result_chars"],
                context.metrics["raw_result_chars"],
            )

    async def test_detail_limit_zero(self):
        async def search(**kwargs):
            self.fail("A blocked detail request must not reach the provider")

        with execution_scope("公园", ExecutionLimits(detail_attempts=0)):
            result = await wrapped_tool(search, "maps_search_detail").ainvoke(
                {"id": "B001"}
            )
            self.assertEqual(result["error_code"], "map_budget_exceeded")

    async def test_route_evidence_is_registered_and_cached(self):
        async def route(**kwargs):
            return {"paths": [{"distance": "1000", "duration": "60"}]}

        tool = wrapped_tool(route, "maps_direction_driving")
        with execution_scope("公园") as context:
            args = {"origin": "120,30", "destination": "121,31"}
            first = await tool.ainvoke(args)
            second = await tool.ainvoke(args)
            identity = first["paths"][0]["route_evidence_id"]
            self.assertEqual(identity, second["paths"][0]["route_evidence_id"])
            self.assertEqual(context.routes[identity].query_destination, "121,31")
            self.assertEqual(context.metrics["map_attempts"], 1)

    def test_route_and_geocode_projection(self):
        with execution_scope("公园") as context:
            route = compact_result(
                {
                    "route": {
                        "paths": [
                            {
                                "distance": "1000",
                                "cost": {"duration": "60"},
                                "steps": ["noise"],
                            }
                        ]
                    }
                },
                "maps_direction_driving",
                context,
            )
            self.assertEqual(route["paths"], [{"distance_m": 1000, "duration_s": 60}])
            geo = compact_result(
                {"geocodes": [{"location": "120,30", "formatted_address": "杭州"}]},
                "maps_geo",
                context,
            )
            self.assertEqual(geo["geocodes"][0]["location"], "120,30")

    def test_invalid_limits(self):
        for values in [
            {"map_attempts": 0},
            {"cache_ttl_seconds": 0},
            {"model_calls": -1},
        ]:
            with self.assertRaises(ValueError):
                ExecutionLimits(**values)
