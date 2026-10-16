"""Offline weather projection, date/area checks, solar events and tool isolation."""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from langchain_core.tools import StructuredTool
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from scene_pilot.core.environment import (
    CHINA_TIME,
    assess_weather,
    calculate_solar,
)
from scene_pilot.core.execution import ExecutionLimits, execution_scope
from scene_pilot.agent import create_planning_graph
from scene_pilot.observability.streaming import StreamObserver
from scene_pilot.core.recommendation import MapEvidence, render_recommendation
from scene_pilot.integrations.amap_mcp import protect_amap_tools
from scene_pilot.integrations.amap_policy import govern_amap_tools
from scene_pilot.integrations.amap_results import compact_result
from scene_pilot.tools.environment_tools import build_conditions_tool
from scene_pilot.tools.photography_tools import prepare_photography_brief
from scene_pilot.tools.recommendation_tools import finalize_recommendation
from test_agent_execution import ScriptedModel, call
from test_amap_policy import wrapped_tool

NOW = datetime(2026, 10, 16, 12, tzinfo=CHINA_TIME)
SHOOTING_DATE = date(2026, 10, 17)


def weather_payload():
    return {
        "status": "1",
        "forecasts": [
            {
                "city": "杭州市",
                "adcode": "330100",
                "reporttime": "2026-10-16 11:00:00",
                "casts": [
                    {
                        "date": "2026-10-17",
                        "dayweather": "晴",
                        "nightweather": "多云",
                        "daytemp": "23",
                        "nighttemp": "15",
                        "daywind": "东",
                        "daypower": "≤3",
                    }
                ],
            }
        ],
    }


class WeatherArguments(BaseModel):
    city: str


def weather_tool(fn, **options):
    raw = StructuredTool.from_function(
        name="maps_weather",
        description="Offline weather",
        coroutine=fn,
        args_schema=WeatherArguments,
    )
    return govern_amap_tools(protect_amap_tools([raw], **options), "private-key")[0]


def prepare(context, *, shooting_date="2026-10-17", identity="poi_test", **fields):
    args = {"city": "杭州", "photography_style": "日系"}
    if shooting_date:
        args["shooting_date"] = shooting_date
    result = prepare_photography_brief.invoke(args)
    if result["status"] != "success":
        raise AssertionError(result)
    context.evidence[identity] = MapEvidence(
        evidence_id=identity,
        tool="maps_text_search",
        retrieved_at=NOW,
        name="测试公园",
        poi_id=identity,
        location="120.15,30.25",
        **fields,
    )


class SolarTests(unittest.TestCase):
    def test_hangzhou_events_order_timezone_and_season(self):
        summer = calculate_solar("120.15,30.25", date(2026, 6, 21))
        winter = calculate_solar("120.15,30.25", date(2026, 12, 21))
        self.assertEqual(summer.status, "available")
        self.assertEqual(summer.sunrise.date(), date(2026, 6, 21))
        self.assertEqual(summer.sunrise.utcoffset(), timedelta(hours=8))
        self.assertTrue(4 <= summer.sunrise.hour <= 6)
        self.assertTrue(18 <= summer.sunset.hour <= 20)
        self.assertLess(summer.morning_golden[0], summer.sunrise)
        self.assertLess(summer.sunrise, summer.morning_golden[1])
        self.assertLess(summer.evening_golden[0], summer.sunset)
        self.assertLess(summer.sunset, summer.evening_golden[1])
        for window in (summer.morning_golden, summer.evening_golden):
            self.assertTrue(all(value.date() == date(2026, 6, 21) for value in window))
        self.assertGreater(
            summer.sunset - summer.sunrise, winter.sunset - winter.sunrise
        )
        self.assertIn("高德坐标", summer.note)
        self.assertIn("遮挡", summer.note)

    def test_polar_day_and_invalid_coordinates(self):
        polar = calculate_solar("20,89", date(2026, 6, 21))
        self.assertEqual(polar.status, "unknown")
        for coordinates in (None, "nan,30", "181,30", "120,91", "120", "wrong"):
            self.assertEqual(
                calculate_solar(coordinates, SHOOTING_DATE).status, "unknown"
            )


class WeatherProjectionTests(unittest.TestCase):
    def test_projection_discards_extra_fields_and_registers_evidence(self):
        payload = weather_payload()
        payload["private"] = "private-key"
        with execution_scope("") as context:
            result = compact_result(payload, "maps_weather", context, ("private-key",))
            self.assertEqual(len(context.weather), 1)
            evidence = next(iter(context.weather.values()))
            self.assertEqual(evidence.report_time.utcoffset(), timedelta(hours=8))
            self.assertEqual(evidence.forecasts[0].daytemp, 23)
            self.assertNotIn("private-key", str(result))
            valid = assess_weather(evidence, SHOOTING_DATE, "330100", None, NOW)
            self.assertEqual(valid.status, "available")

    def test_area_date_and_freshness_must_match(self):
        with execution_scope("") as context:
            compact_result(weather_payload(), "maps_weather", context)
            evidence = next(iter(context.weather.values()))
            for target_date, adcode, now in (
                (SHOOTING_DATE, "110100", NOW),
                (date(2026, 10, 25), "330100", NOW),
                (SHOOTING_DATE, "330100", NOW + timedelta(days=2)),
                (SHOOTING_DATE, "330100", NOW - timedelta(days=1)),
            ):
                self.assertEqual(
                    assess_weather(evidence, target_date, adcode, None, now).status,
                    "unknown",
                )
            city_evidence = evidence.model_copy(update={"query_area": "杭州市"})
            self.assertEqual(
                assess_weather(
                    city_evidence, SHOOTING_DATE, None, "杭州市", NOW
                ).status,
                "available",
            )
            self.assertEqual(
                assess_weather(evidence, SHOOTING_DATE, None, "杭州", NOW).status,
                "unknown",
            )

    def test_partial_malformed_response_is_atomic(self):
        payload = weather_payload()
        payload["forecasts"].append({"city": "bad"})
        with execution_scope("") as context:
            with self.assertRaises(ValueError):
                compact_result(payload, "maps_weather", context)
            self.assertEqual(context.weather, {})

    def test_duplicate_day_nonfinite_and_secret_fields_rejected(self):
        for field, value in (
            ("daytemp", "nan"),
            ("daytemp", True),
            ("dayweather", "private-key"),
        ):
            payload = weather_payload()
            payload["forecasts"][0]["casts"][0][field] = value
            with execution_scope("") as context, self.assertRaises(ValueError):
                compact_result(payload, "maps_weather", context, ("private-key",))
        payload = weather_payload()
        payload["forecasts"][0]["casts"] *= 2
        with execution_scope("") as context, self.assertRaises(ValueError):
            compact_result(payload, "maps_weather", context)

    def test_current_conditions_are_not_forecasts(self):
        with execution_scope("") as context:
            result = compact_result(
                {"lives": [{"weather": "晴"}]}, "maps_weather", context
            )
            self.assertEqual(result["forecasts"], [])
            self.assertEqual(context.weather, {})

    def test_official_mcp_flat_shape_keeps_missing_publication_time_unknown(self):
        flat = {"city": "杭州", "forecasts": weather_payload()["forecasts"][0]["casts"]}
        with execution_scope("") as context:
            compact_result(flat, "maps_weather", context, query={"city": "杭州市"})
            evidence = next(iter(context.weather.values()))
            self.assertIsNone(evidence.report_time)
            self.assertIsNone(evidence.adcode)
            result = assess_weather(evidence, SHOOTING_DATE, "330106", "杭州市", NOW)
            self.assertEqual(result.status, "reference")
            self.assertEqual(result.forecast.daytemp, 23)
            self.assertIn("时效性未知", result.reason)
            self.assertEqual(
                assess_weather(evidence, SHOOTING_DATE, "110100", "北京市", NOW).status,
                "unknown",
            )

    def test_city_forecast_can_cover_district_poi_only_when_city_matches(self):
        with execution_scope("") as context:
            compact_result(
                weather_payload(), "maps_weather", context, query={"city": "杭州市"}
            )
            evidence = next(iter(context.weather.values()))
            self.assertEqual(
                assess_weather(evidence, SHOOTING_DATE, "330106", "杭州市", NOW).status,
                "available",
            )
            self.assertEqual(
                assess_weather(evidence, SHOOTING_DATE, "330106", "宁波市", NOW).status,
                "unknown",
            )
            wrong_code = evidence.model_copy(update={"adcode": "110100"})
            self.assertEqual(
                assess_weather(
                    wrong_code, SHOOTING_DATE, "330106", "杭州市", NOW
                ).status,
                "unknown",
            )

    def test_search_query_city_is_provenance_not_poi_city(self):
        poi = {"id": "B001", "name": "测试公园", "location": "120.15,30.25"}
        with execution_scope("") as context:
            result = compact_result(
                {"pois": [poi]}, "maps_text_search", context, query={"city": "杭州"}
            )
            item = result["pois"][0]
            self.assertIsNone(item["city"])
            self.assertEqual(item["query_city"], "杭州")
            detail = compact_result({"pois": [poi]}, "maps_search_detail", context)
            self.assertEqual(detail["pois"][0]["query_city"], "杭州")
            compact_result(
                {"pois": [poi]}, "maps_text_search", context, query={"city": "北京"}
            )
            ambiguous = compact_result({"pois": [poi]}, "maps_search_detail", context)
            self.assertIsNone(ambiguous["pois"][0]["query_city"])


class ConditionsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch("scene_pilot.tools.environment_tools.datetime")
        self.mock_clock = self.clock.start()
        self.mock_clock.now.return_value = NOW.astimezone(UTC)
        self.addCleanup(self.clock.stop)

    async def test_three_candidates_share_one_weather_call_and_final_has_sources(self):
        calls = []

        async def provider(city):
            calls.append(city)
            return weather_payload()

        tool = build_conditions_tool([weather_tool(provider)])
        with execution_scope("杭州日系 2026-10-17") as context:
            for identity in ("one", "two", "three"):
                prepare(context, identity=identity, adcode="330100")
            result = await tool.ainvoke({"evidence_ids": ["one", "two", "three"]})
            self.assertEqual(result["status"], "success")
            self.assertEqual(calls, ["330100"])
            self.assertEqual(context.metrics["weather_attempts"], 1)
            self.assertEqual(context.metrics["cache_hits"], 2)
            self.assertEqual(context.metrics["solar_calculations"], 3)
            await tool.ainvoke({"evidence_ids": ["one"]})
            self.assertEqual(context.metrics["solar_calculations"], 3)
            result = finalize_recommendation.invoke(
                {
                    "user_statements": [{"quote": "杭州"}],
                    "candidates": [{"evidence_id": "one"}],
                }
            )
            self.assertEqual(result["status"], "success")
            output = render_recommendation(context.recommendation)
            self.assertIn("白天 晴 23℃", output)
            self.assertIn("黄金时段", output)
            self.assertIn("maps_weather / weather_", output)
            self.assertIn("未考虑云层", output)
        with execution_scope("杭州日系 2026-10-17") as context:
            self.assertEqual(context.conditions, {})
            self.assertIsNone(context.brief)
            denied = await tool.ainvoke({"evidence_ids": ["one"]})
            self.assertEqual(denied["status"], "error")

    async def test_no_date_no_network_or_solar_guess(self):
        async def provider(city):
            self.fail("No date must not spend weather quota")

        with execution_scope("杭州周六日系") as context:
            prepare(context, shooting_date=None, adcode="330100")
            tool = build_conditions_tool([weather_tool(provider)])
            await tool.ainvoke({"evidence_ids": ["poi_test"]})
            report = context.conditions["poi_test"]
            self.assertIsNone(report.solar)
            self.assertEqual(report.weather.status, "unknown")
            self.assertEqual(context.metrics["map_attempts"], 0)

    async def test_missing_weather_capability_or_area_preserves_solar(self):
        for fields in ({"adcode": "330100"}, {}):
            with execution_scope("杭州 2026-10-17") as context:
                prepare(context, **fields)
                await build_conditions_tool([]).ainvoke({"evidence_ids": ["poi_test"]})
                report = context.conditions["poi_test"]
                self.assertEqual(report.solar.status, "available")
                self.assertEqual(report.weather.status, "unknown")

    async def test_bad_response_fails_closed_and_counts_attempt(self):
        for payload in (
            "晴天适合拍照 private-key",
            {"status": "0", "info": "private-key"},
        ):

            async def provider(city):
                return payload

            with execution_scope("杭州 2026-10-17") as context:
                prepare(context, adcode="330100")
                result = await build_conditions_tool([weather_tool(provider)]).ainvoke(
                    {"evidence_ids": ["poi_test"]}
                )
                self.assertNotIn("private-key", str(result))
                self.assertEqual(
                    context.conditions["poi_test"].weather.status, "unknown"
                )
                self.assertEqual(context.metrics["weather_attempts"], 1)

    async def test_weather_retry_shares_total_and_weather_budget(self):
        async def provider(city):
            raise ConnectionError("private-key")

        for limits in (
            ExecutionLimits(weather_attempts=1),
            ExecutionLimits(map_attempts=1),
        ):
            with execution_scope("杭州 2026-10-17", limits) as context:
                prepare(context, adcode="330100")
                tool = build_conditions_tool(
                    [weather_tool(provider, retry_delay_seconds=0)]
                )
                await tool.ainvoke({"evidence_ids": ["poi_test"]})
                self.assertEqual(context.metrics["map_attempts"], 1)
                self.assertEqual(context.metrics["budget_rejections"], 1)

    async def test_cancellation_preserves_local_solar_and_propagates(self):
        started = asyncio.Event()

        async def provider(city):
            started.set()
            await asyncio.sleep(10)

        with execution_scope("杭州 2026-10-17") as context:
            prepare(context, adcode="330100")
            tool = build_conditions_tool([weather_tool(provider)])
            task = asyncio.create_task(tool.ainvoke({"evidence_ids": ["poi_test"]}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(context.conditions["poi_test"].solar.status, "available")
            self.assertEqual(context.conditions["poi_test"].weather.status, "unknown")

    async def test_candidate_limit_duplicate_ids_and_unknown_ids(self):
        with execution_scope("杭州 2026-10-17") as context:
            for identity in ("one", "two", "three", "four"):
                prepare(context, identity=identity)
            tool = build_conditions_tool([])
            for identities in (["one", "one"], ["fabricated"]):
                self.assertEqual(
                    (await tool.ainvoke({"evidence_ids": identities}))["status"],
                    "error",
                )
            await tool.ainvoke({"evidence_ids": ["one", "two", "three"]})
            result = await tool.ainvoke({"evidence_ids": ["four"]})
            self.assertEqual(result["error_code"], "condition_candidate_limit")

    async def test_explicit_date_and_immutable_brief(self):
        with execution_scope("杭州周六") as context:
            result = prepare_photography_brief.invoke(
                {
                    "city": "杭州",
                    "photography_style": "日系",
                    "shooting_date": "2026-10-17",
                }
            )
            self.assertEqual(result["error_code"], "explicit_date_required")
            self.assertIsNone(context.brief)
        with execution_scope("杭州 2026-10-17") as context:
            prepare(context)
            result = prepare_photography_brief.invoke(
                {"city": "北京", "photography_style": "日系"}
            )
            self.assertEqual(result["error_code"], "brief_already_prepared")
            self.assertEqual(context.brief.city, "杭州")

    async def test_actual_graph_prepares_delegates_inspects_and_finalizes(self):
        async def search(**kwargs):
            return {
                "pois": [
                    {
                        "id": "B001",
                        "name": "测试公园",
                        "location": "120.15,30.25",
                    }
                ]
            }

        async def provider(city):
            self.assertEqual(city, "杭州市")
            return {
                "city": "杭州",
                "forecasts": weather_payload()["forecasts"][0]["casts"],
            }

        def script(messages):
            tool_messages = [message for message in messages if message.type == "tool"]
            if "你是摄影场景调研专员" in messages[0].content:
                if not tool_messages:
                    return call(
                        "maps_text_search",
                        {"keywords": "公园", "city": "杭州市"},
                        "search",
                    )
                return AIMessage(content=tool_messages[-1].content)
            if not tool_messages:
                return call(
                    "prepare_photography_brief",
                    {
                        "city": "杭州",
                        "photography_style": "日系",
                        "shooting_date": "2026-10-17",
                    },
                    "brief",
                )
            last = tool_messages[-1]
            if last.name == "prepare_photography_brief":
                return call(
                    "task",
                    {
                        "subagent_type": "scene-agent",
                        "description": "查杭州公园日系人像地点",
                    },
                    "delegate",
                )
            if last.name == "task":
                identity = json.loads(last.content)["pois"][0]["evidence_id"]
                return call(
                    "inspect_shooting_conditions",
                    {"evidence_ids": [identity]},
                    "conditions",
                )
            if last.name == "inspect_shooting_conditions":
                identity = json.loads(last.content)["conditions"][0]["poi_evidence_id"]
                return call(
                    "finalize_recommendation",
                    {
                        "user_statements": [{"quote": "杭州"}],
                        "candidates": [{"evidence_id": identity}],
                    },
                    "final",
                )
            return AIMessage(content="伪造：现场一定有阳光，无需看天气")

        model = ScriptedModel(script=script)
        graph = create_planning_graph(
            model, [wrapped_tool(search)], [], [weather_tool(provider)]
        )
        with tempfile.TemporaryDirectory() as directory:
            observer = StreamObserver(Path(directory), emit=lambda _: None)
            result = await observer.arun(graph, "杭州日系 2026-10-17")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.metrics["weather_attempts"], 1)
            self.assertEqual(result.metrics["map_attempts"], 2)
            self.assertIn("时效性未知", result.final_text)
            self.assertIn("搜索城市参考天气", result.final_text)
            self.assertIn("行政归属未核实", result.final_text)
            self.assertIn("晨间黄金时段", result.final_text)
            self.assertNotIn("现场一定有阳光", result.final_text)
            self.assertFalse(
                any("maps_weather" in names for names in model.bound_toolsets)
            )
            trace = observer.trace_path.read_text(encoding="utf-8")
            self.assertNotIn("2026-10-17", trace)
            self.assertNotIn("测试公园", trace)

    async def test_past_date_skips_network_and_timeout_keeps_solar(self):
        async def forbidden(city):
            self.fail("Past shooting date must not query today's weather")

        with execution_scope("杭州 2026-10-15") as context:
            prepare(context, shooting_date="2026-10-15", adcode="330100")
            await build_conditions_tool([weather_tool(forbidden)]).ainvoke(
                {"evidence_ids": ["poi_test"]}
            )
            self.assertEqual(context.metrics["weather_attempts"], 0)
            self.assertIn("过去", context.conditions["poi_test"].weather.reason)

        async def slow(city):
            await asyncio.sleep(10)

        with execution_scope("杭州 2026-10-17") as context:
            prepare(context, adcode="330100")
            tool = build_conditions_tool(
                [weather_tool(slow, timeout_seconds=0.001, max_retries=0)]
            )
            await tool.ainvoke({"evidence_ids": ["poi_test"]})
            self.assertEqual(context.conditions["poi_test"].solar.status, "available")
            self.assertEqual(context.conditions["poi_test"].weather.status, "unknown")
