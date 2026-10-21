"""Confirmation is a human/host boundary, tested without service calls."""

import unittest
import tempfile
from pathlib import Path
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from scene_pilot.briefing import DraftResult, extract_brief
from scene_pilot.cli_confirmation import review_brief, choose_departure
from scene_pilot.core.confirmation import BriefDraft, PreparationUsage, confirm_brief
from scene_pilot.core.execution import ExecutionLimits, execution_scope
from scene_pilot.core.photography import PhotographyBrief
from scene_pilot.tools.photography_tools import prepare_photography_brief


def confirmed(request="原始需求", **values):
    brief = PhotographyBrief.model_validate(
        {
            "city": "杭州",
            "photography_style": "日系",
            "departure": "杭州东站西广场",
            "transport_mode": "驾车",
            "max_travel_minutes": 40,
            **values,
        }
    )
    return confirm_brief(request, brief, accepted=True)


class ConfirmationTests(unittest.TestCase):
    def test_acceptance_and_required_fields(self):
        for accepted in [False, None, "true"]:
            with self.assertRaises(ValueError):
                confirm_brief("请求", BriefDraft(), accepted=accepted)
        with self.assertRaises(ValueError):
            confirm_brief("请求", BriefDraft(), accepted=True)
        with self.assertRaises(ValueError):
            BriefDraft(city="杭州", photography_style="日系", confirmed=True)

    def test_snapshot_is_immutable_and_isolated_from_lists(self):
        brief = PhotographyBrief(
            city="杭州", photography_style="日系", equipment=["镜头"]
        )
        snapshot = confirm_brief("请求", brief, accepted=True)
        brief.city = "上海"
        brief.equipment.append("闪光灯")
        snapshot.brief.equipment.clear()
        self.assertEqual(snapshot.brief.city, "杭州")
        self.assertEqual(snapshot.brief.equipment, ["镜头"])
        with self.assertRaises(FrozenInstanceError):
            snapshot.request = "改变请求"

    def test_snapshot_cannot_be_reused_for_another_request_or_leak_between_runs(self):
        snapshot = confirmed()
        with self.assertRaises(ValueError):
            with execution_scope("其他请求", confirmed_brief=snapshot):
                pass
        with self.assertRaises(ValueError):
            with execution_scope("原始需求", confirmed_brief={"accepted": True}):
                pass
        with execution_scope("原始需求", confirmed_brief=snapshot) as context:
            self.assertEqual(context.brief, snapshot.brief)
        with execution_scope("原始需求") as context:
            self.assertIsNone(context.confirmed_brief)
            self.assertIsNone(context.brief)
            self.assertIsNone(context.departure)

    def test_tool_cannot_modify_confirmation_but_user_can_confirm_new_date(self):
        snapshot = confirmed(shooting_date="2026-11-01")
        with execution_scope("原始需求", confirmed_brief=snapshot) as context:
            values = snapshot.brief.model_dump(mode="json")
            self.assertEqual(
                prepare_photography_brief.invoke(values)["status"], "success"
            )
            result = prepare_photography_brief.invoke(
                {**values, "max_travel_minutes": 99}
            )
            self.assertEqual(result["error_code"], "confirmed_brief_immutable")
            self.assertEqual(context.brief.max_travel_minutes, 40)

    def test_cli_edits_invalidates_preview_and_needs_explicit_acceptance(self):
        replies = iter(
            ["11", "-1", "11", "55", "4", "2026-11-01", "10", "公交", "确认"]
        )
        output = []
        result = review_brief(
            BriefDraft(city="杭州", photography_style="日系", max_travel_minutes=40),
            read=lambda _: next(replies),
            emit=output.append,
        )
        self.assertEqual(result.max_travel_minutes, 55)
        self.assertEqual(result.transport_mode, "公交")
        self.assertEqual(result.shooting_date.isoformat(), "2026-11-01")
        self.assertTrue(any("原值已保留" in line for line in output))
        self.assertTrue(any("55" in line and "单程" in line for line in output))

    def test_cli_missing_city_cannot_confirm_and_cancel_does_not_accept(self):
        replies = iter(["确认", "1", "杭州", "2", "日系", "取消"])
        self.assertIsNone(
            review_brief(
                BriefDraft(), read=lambda _: next(replies), emit=lambda _: None
            )
        )

    def test_cli_clear_and_zero_are_distinct(self):
        replies = iter(["11", "0", "12", "清空", "确认"])
        result = review_brief(
            BriefDraft(city="杭州", photography_style="日系", max_budget_cny=200),
            read=lambda _: next(replies),
            emit=lambda _: None,
        )
        self.assertEqual(result.max_travel_minutes, 0)
        self.assertIsNone(result.max_budget_cny)

    def test_departure_selection_requires_number_even_for_one_result(self):
        options = [
            SimpleNamespace(
                formatted_address="地址", location="120,30", evidence_id="geo"
            )
        ]
        replies = iter(["", "确认", "1"])
        self.assertEqual(
            choose_departure(
                options, read=lambda _: next(replies), emit=lambda _: None
            ),
            "geo",
        )
        self.assertIsNone(
            choose_departure(options, read=lambda _: "跳过", emit=lambda _: None)
        )


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    def model(self, args):
        response = AIMessage(
            content="",
            tool_calls=[{"name": "BriefDraft", "args": args, "id": "draft"}],
            usage_metadata={
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
        )
        bound = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        return SimpleNamespace(bind_tools=lambda *a, **k: bound), bound

    async def test_single_call_extracts_only_draft_and_preserves_missing_fields(self):
        model, bound = self.model({"clothing": "白裙", "shooting_date": "2026-11-01"})
        result = await extract_brief(model, "明天穿白裙")
        bound.ainvoke.assert_awaited_once()
        self.assertIsNone(result.draft.city)
        self.assertIsNone(result.draft.shooting_date)
        self.assertEqual(result.usage.input_tokens, 20)
        with self.assertRaises(ValueError):
            confirm_brief("明天穿白裙", result.draft, accepted=True)

    async def test_model_cannot_self_confirm_via_extra_arguments(self):
        model, _ = self.model(
            {"city": "杭州", "photography_style": "日系", "confirmed": True}
        )
        result = await extract_brief(model, "杭州日系")
        self.assertEqual(result.error_code, "brief_extraction_failed")

    async def test_provider_error_is_sanitized_without_retry_and_attempt_is_charged(
        self,
    ):
        model, bound = self.model({})
        bound.ainvoke.side_effect = RuntimeError("api_key=private-secret")
        result = await extract_brief(model, "私密需求")
        self.assertNotIn("private-secret", str(result))
        self.assertEqual(
            result.usage, PreparationUsage(model_attempts=1, usage_missing=1)
        )
        bound.ainvoke.assert_awaited_once()

    async def test_long_input_does_not_call_model(self):
        model, bound = self.model({})
        result = await extract_brief(model, "12345", ExecutionLimits(max_input_chars=4))
        bound.ainvoke.assert_not_awaited()
        self.assertEqual(result.error_code, "request_too_long")

    async def test_cli_cancel_does_not_connect_maps_or_plan(self):
        from scene_pilot.main import run_cli

        with (
            patch(
                "scene_pilot.main.get_settings",
                return_value=SimpleNamespace(qwen_api_key="test", amap_web_key="test"),
            ),
            patch("scene_pilot.main.create_qwen_model"),
            patch(
                "scene_pilot.main.extract_brief",
                new=AsyncMock(
                    return_value=DraftResult(
                        BriefDraft(), PreparationUsage(model_attempts=1)
                    )
                ),
            ),
            patch("scene_pilot.main.review_brief", return_value=None),
            patch("scene_pilot.main.load_amap_tools", new=AsyncMock()) as maps,
            patch("scene_pilot.main.StreamObserver") as observer,
            patch("builtins.input", side_effect=["杭州日系", "exit"]),
            patch("builtins.print"),
        ):
            await run_cli()
        maps.assert_not_awaited()
        observer.assert_not_called()

    async def test_preparation_spends_shared_model_and_token_budget_before_maps(self):
        from scene_pilot.observability.streaming import StreamObserver

        snapshot = confirm_brief(
            "原始需求",
            confirmed().brief,
            accepted=True,
            preparation_usage=PreparationUsage(model_attempts=1, input_tokens=100),
        )
        for limits in [
            ExecutionLimits(model_calls=1),
            ExecutionLimits(observed_tokens=100),
        ]:
            geo = SimpleNamespace(ainvoke=AsyncMock())
            with tempfile.TemporaryDirectory() as directory:
                result = await StreamObserver(
                    Path(directory), emit=lambda _: None, limits=limits
                ).arun(
                    None,
                    "原始需求",
                    confirmed_brief=snapshot,
                    geocode_tool=geo,
                    select_origin=AsyncMock(),
                )
            self.assertEqual(result.status, "budget_exhausted")
            self.assertEqual(result.metrics["model_attempts"], 1)
            geo.ainvoke.assert_not_awaited()

    async def test_edited_brief_cannot_bypass_input_size_limit(self):
        from scene_pilot.observability.streaming import StreamObserver

        snapshot = confirmed(equipment=["器材" * 7000])
        with tempfile.TemporaryDirectory() as directory:
            result = await StreamObserver(Path(directory), emit=lambda _: None).arun(
                None,
                "原始需求",
                confirmed_brief=snapshot,
            )
        self.assertEqual(result.status, "budget_exhausted")

    async def test_cli_model_constructor_failure_is_sanitized(self):
        from scene_pilot.main import run_cli

        with (
            patch(
                "scene_pilot.main.get_settings",
                return_value=SimpleNamespace(qwen_api_key="test", amap_web_key="test"),
            ),
            patch(
                "scene_pilot.main.create_qwen_model",
                side_effect=ValueError("api_key=private-secret"),
            ),
            patch("builtins.print") as output,
        ):
            await run_cli()
        self.assertNotIn("private-secret", str(output.call_args_list))
        self.assertIn("初始化失败", str(output.call_args_list))
