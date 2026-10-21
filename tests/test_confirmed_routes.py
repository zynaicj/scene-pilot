"""Origin binding, transport constraints and partial output stay evidence based."""

import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from test_amap_policy import POI, wrapped_tool
from test_confirmation import confirmed

from scene_pilot.core.confirmation import resolve_departure
from scene_pilot.core.execution import ExecutionLimits, execution_scope
from scene_pilot.core.recommendation import CandidateDraft, assemble_recommendation
from scene_pilot.integrations.amap_mcp import protect_amap_tools
from scene_pilot.integrations.amap_policy import govern_amap_tools
from scene_pilot.integrations.amap_results import compact_result
from scene_pilot.observability.streaming import StreamObserver


class GeoArgs(BaseModel):
    address: str
    city: str


def geocoder(payload=None):
    provider = AsyncMock(
        return_value=payload
        or {
            "geocodes": [
                {"location": "119,30", "formatted_address": "杭州东站西广场"},
                {"location": "119.1,30.1", "formatted_address": "杭州东站其他位置"},
            ]
        }
    )

    async def geo(address, city):
        return await provider(address=address, city=city)

    raw = StructuredTool.from_function(
        name="maps_geo",
        description="Offline geocode",
        args_schema=GeoArgs,
        coroutine=geo,
    )
    return govern_amap_tools(protect_amap_tools([raw]), "fake-secret")[0], provider


async def first(options):
    return options[0].evidence_id


class ConfirmedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def setup_route(self, context, seconds=3300):
        tool, _ = geocoder()
        await resolve_departure(context, tool, first)
        pois = compact_result({"pois": [POI]}, "maps_text_search", context)
        destination = pois["pois"][0]

        async def driving(**kwargs):
            return {"paths": [{"distance": 1000, "duration": seconds}]}

        result = await wrapped_tool(driving, "maps_direction_driving").ainvoke(
            {"origin": "119.0,30.0", "destination": destination["location"]}
        )
        route_id = result["paths"][0]["route_evidence_id"]
        return destination["evidence_id"], route_id

    def plan(self, context, poi_id, route_id=None):
        return assemble_recommendation(
            context.request,
            [],
            [CandidateDraft(evidence_id=poi_id, route_evidence_id=route_id)],
            context.evidence,
            context.routes,
            confirmed_brief=context.confirmed_brief,
            departure=context.departure,
        )

    async def test_ambiguous_geocode_is_never_automatically_selected(self):
        for selected in [None, "foreign-id"]:
            with execution_scope("原始需求", confirmed_brief=confirmed()) as context:
                geo, provider = geocoder()
                selector = AsyncMock(return_value=selected)
                await resolve_departure(context, geo, selector)
                provider.assert_awaited_once_with(address="杭州东站西广场", city="杭州")
                selector.assert_awaited_once()
                self.assertEqual(len(selector.call_args.args[0]), 2)
                self.assertIsNone(context.departure)
                self.assertEqual(context.metrics["map_attempts"], 1)

    async def test_mode_or_missing_departure_skips_geocoding(self):
        for values in [
            {"transport_mode": "公交"},
            {"transport_mode": "步行"},
            {"transport_mode": None},
            {"departure": None},
        ]:
            with execution_scope(
                "原始需求", confirmed_brief=confirmed(**values)
            ) as context:
                geo, provider = geocoder()
                await resolve_departure(context, geo, first)
                provider.assert_not_awaited()

    async def test_wrong_address_or_city_denied_before_provider(self):
        with execution_scope("原始需求", confirmed_brief=confirmed()) as context:
            geo, provider = geocoder()
            for args in [
                {"address": "其他地址", "city": "杭州"},
                {"address": "杭州东站西广场", "city": "上海"},
            ]:
                self.assertEqual(
                    (await geo.ainvoke(args))["error_code"], "confirmed_route_required"
                )
            self.assertEqual(context.metrics["map_attempts"], 0)
            provider.assert_not_awaited()

    async def test_wrong_origin_destination_or_mode_cannot_spend_quota(self):
        async def forbidden(**kwargs):
            self.fail("Invalid route must not reach upstream")

        tool = wrapped_tool(forbidden, "maps_direction_driving")
        with execution_scope("原始需求", confirmed_brief=confirmed()) as context:
            poi_id, _ = await self.setup_route(context)
            count = context.metrics["map_attempts"]
            for args in [
                {"origin": "119.1,30.1", "destination": POI["location"]},
                {"origin": "119,30", "destination": "122,32"},
                {"origin": "nan,30", "destination": POI["location"]},
            ]:
                self.assertEqual((await tool.ainvoke(args))["status"], "error")
            self.assertEqual(context.metrics["map_attempts"], count)
            self.assertEqual(
                self.plan(context, poi_id).candidates[0].travel_time.status, "not_met"
            )
        for mode in [None, "步行", "公交"]:
            with execution_scope(
                "原始需求", confirmed_brief=confirmed(transport_mode=mode)
            ) as context:
                self.assertEqual(
                    (
                        await tool.ainvoke(
                            {"origin": "119,30", "destination": POI["location"]}
                        )
                    )["status"],
                    "error",
                )
                self.assertEqual(context.metrics["map_attempts"], 0)

    async def test_time_limit_exact_seconds_zero_and_known_violation_cannot_be_omitted(
        self,
    ):
        for limit, seconds, status in [
            (40, 2400, "meets"),
            (40, 2401, "not_met"),
            (40, 3300, "not_met"),
            (0, 0, "meets"),
            (0, 1, "not_met"),
        ]:
            with execution_scope(
                "原始需求", confirmed_brief=confirmed(max_travel_minutes=limit)
            ) as context:
                poi_id, route_id = await self.setup_route(context, seconds)
                for included in [None, route_id]:
                    check = (
                        self.plan(context, poi_id, included).candidates[0].travel_time
                    )
                    self.assertEqual(check.status, status)
                    self.assertEqual(check.route_evidence_id, route_id)
                    self.assertEqual(check.estimated_minutes, seconds / 60)

    async def test_unconfirmed_or_missing_limit_is_unknown(self):
        for snapshot in [None, confirmed(max_travel_minutes=None)]:
            with execution_scope("原始需求", confirmed_brief=snapshot) as context:
                if snapshot:
                    poi_id, route_id = await self.setup_route(context)
                else:
                    pois = compact_result({"pois": [POI]}, "maps_text_search", context)
                    poi_id, route_id = pois["pois"][0]["evidence_id"], None
                self.assertEqual(
                    self.plan(context, poi_id, route_id)
                    .candidates[0]
                    .travel_time.status,
                    "unknown",
                )

    async def test_finalization_rejects_wrong_origin_or_confirmation(self):
        with execution_scope("原始需求", confirmed_brief=confirmed()) as context:
            poi_id, route_id = await self.setup_route(context)
            original = context.routes[route_id]
            for update in [
                {"query_origin": "119.1,30.1"},
                {"origin_evidence_id": "fake"},
                {"brief_confirmation_id": "another-confirmation"},
            ]:
                context.routes[route_id] = original.model_copy(update=update)
                with self.assertRaises(ValueError):
                    self.plan(context, poi_id, route_id)
                self.assertEqual(
                    self.plan(context, poi_id).candidates[0].travel_time.status,
                    "unknown",
                )

    async def test_geocode_failure_does_not_block_candidates_and_does_not_leak(self):
        geo, provider = geocoder({"status": "0", "info": "fake-secret"})

        class EvidenceOnlyAgent:
            async def astream(self, *args, **kwargs):
                from scene_pilot.core.execution import current_run

                compact_result({"pois": [POI]}, "maps_text_search", current_run())
                yield {}

        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(Path(directory), emit=lambda _: None).arun(
                EvidenceOnlyAgent(),
                "原始需求",
                confirmed_brief=confirmed(),
                geocode_tool=geo,
                select_origin=first,
            )
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(
                result.recommendation["candidates"][0]["travel_time"]["status"],
                "unknown",
            )
            trace = result.trace_path.read_text(encoding="utf-8")
            for private in ["fake-secret", "杭州东站", "原始需求", "119,30"]:
                self.assertNotIn(private, trace)
        provider.assert_awaited_once()

    async def test_origin_attempt_counts_towards_total_budget(self):
        geo, _ = geocoder()
        with execution_scope(
            "原始需求", ExecutionLimits(map_attempts=1), confirmed_brief=confirmed()
        ) as context:
            await resolve_departure(context, geo, first)

            async def forbidden(**kwargs):
                self.fail("Geocode already spent the map budget")

            result = await wrapped_tool(forbidden).ainvoke({"keywords": "公园"})
            self.assertEqual(result["error_code"], "map_budget_exceeded")

    async def test_partial_result_preserves_bound_route_and_constraint(self):
        owner = self

        class PartialAgent:
            async def astream(self, *args, **kwargs):
                from scene_pilot.core.execution import current_run

                await owner.setup_route(current_run())
                yield {}

        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(Path(directory), emit=lambda _: None).arun(
                PartialAgent(),
                "原始需求",
                confirmed_brief=confirmed(),
            )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            result.recommendation["candidates"][0]["travel_time"]["status"], "not_met"
        )
        self.assertIn("55 分钟 / 上限 40 分钟", result.final_text)

    async def test_human_wait_does_not_use_active_execution_timeout(self):
        geo, _ = geocoder()

        async def slow_choice(options):
            await asyncio.sleep(0.12)
            return options[0].evidence_id

        class EmptyAgent:
            async def astream(self, *args, **kwargs):
                yield {}

        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(
                Path(directory),
                emit=lambda _: None,
                limits=ExecutionLimits(timeout_seconds=0.1),
            ).arun(
                EmptyAgent(),
                "原始需求",
                confirmed_brief=confirmed(),
                geocode_tool=geo,
                select_origin=slow_choice,
            )
        self.assertEqual(result.status, "incomplete")
        self.assertIsNotNone(result.recommendation["confirmed_departure"])
