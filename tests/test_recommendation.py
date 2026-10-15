import unittest
from datetime import UTC, datetime

from scene_pilot.core.execution import execution_scope
from scene_pilot.core.recommendation import (
    CandidateDraft,
    DrivingRouteEvidence,
    MapEvidence,
    PhotographyAdvice,
    UnknownVenueInfo,
    UserStatement,
    assemble_recommendation,
    render_recommendation,
)
from scene_pilot.tools.recommendation_tools import finalize_recommendation


def evidence():
    return MapEvidence(
        evidence_id="poi_test",
        tool="maps_text_search",
        retrieved_at=datetime.now(UTC),
        poi_id="B001",
        name="测试公园",
        location="120,30",
    )


class RecommendationTests(unittest.TestCase):
    def test_separate_user_advice_facts_unknown(self):
        facts = evidence()
        plan = assemble_recommendation(
            "白色连衣裙，淡妆",
            [UserStatement(quote="淡妆")],
            [
                CandidateDraft(
                    evidence_id=facts.evidence_id,
                    advice=[PhotographyAdvice(text="建议柔光拍摄")],
                )
            ],
            {facts.evidence_id: facts},
        )
        item = plan.candidates[0]
        self.assertEqual(item.advice[0].source, "ai_inference")
        self.assertIsNone(item.facts.address)
        self.assertEqual(item.unknown.status, "unverified")
        self.assertIn("ticket_price", item.unknown.fields)
        self.assertIn("小红书/抖音", render_recommendation(plan))

    def test_reject_invented_user_details(self):
        with self.assertRaises(ValueError):
            assemble_recommendation(
                "白色连衣裙", [UserStatement(quote="丝绸连衣裙")], [], {}
            )

    def test_reject_unknown_evidence_and_duplicate_places(self):
        facts = evidence()
        for drafts in [
            [CandidateDraft(evidence_id="fabricated")],
            [CandidateDraft(evidence_id=facts.evidence_id)] * 2,
        ]:
            with self.assertRaises(ValueError):
                assemble_recommendation("", [], drafts, {facts.evidence_id: facts})

    def test_cannot_inject_verified_price_or_remove_unknown(self):
        with self.assertRaises(ValueError):
            MapEvidence(**{**evidence().model_dump(), "ticket_price": 0})
        with self.assertRaises(ValueError):
            UnknownVenueInfo(status="verified")
        with self.assertRaises(ValueError):
            UnknownVenueInfo(fields=())

    def test_finalize_validates_against_run_registry(self):
        with execution_scope("淡妆") as context:
            context.evidence["poi_test"] = evidence()
            result = finalize_recommendation.invoke(
                {
                    "user_statements": [{"quote": "淡妆"}],
                    "candidates": [{"evidence_id": "poi_test"}],
                }
            )
            self.assertEqual(result["status"], "success")
            self.assertIsNotNone(context.recommendation)
        with execution_scope("淡妆"):
            result = finalize_recommendation.invoke(
                {
                    "user_statements": [],
                    "candidates": [{"evidence_id": "poi_test"}],
                }
            )
            self.assertEqual(result["status"], "error")

    def test_max_three_candidates(self):
        records = {
            str(i): evidence().model_copy(
                update={"evidence_id": str(i), "poi_id": str(i)}
            )
            for i in range(4)
        }
        with self.assertRaises(ValueError):
            assemble_recommendation(
                "", [], [CandidateDraft(evidence_id=key) for key in records], records
            )

    def test_route_must_match_candidate_destination(self):
        facts = evidence()
        route = DrivingRouteEvidence(
            evidence_id="route",
            retrieved_at=datetime.now(UTC),
            query_origin="119,29",
            query_destination="120,30",
            distance_m=1000,
            duration_s=60,
        )
        draft = CandidateDraft(evidence_id="poi_test", route_evidence_id="route")
        plan = assemble_recommendation(
            "", [], [draft], {"poi_test": facts}, {"route": route}
        )
        self.assertIn("1 分钟", render_recommendation(plan))
        with self.assertRaises(ValueError):
            assemble_recommendation(
                "",
                [],
                [draft],
                {"poi_test": facts},
                {"route": route.model_copy(update={"query_destination": "121,31"})},
            )
