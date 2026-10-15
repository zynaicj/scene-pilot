"""摄影需求约束测试；不需要模型或地图 Key。"""
import unittest
from datetime import date
from pydantic import ValidationError
from scene_pilot.core.photography import PhotographyBrief, make_verification_queries


class PhotographyBriefTests(unittest.TestCase):
    def test_minimum_brief(self):
        brief = PhotographyBrief(city=" 杭州 ", photography_style=" 日系清新 ")
        self.assertEqual(brief.city, "杭州")
        self.assertEqual(brief.photography_style, "日系清新")
        self.assertIsNone(brief.max_budget_cny)
        self.assertIsNone(brief.shooting_date)
        self.assertEqual(brief.desired_scenes, [])

    def test_extended_brief(self):
        brief = PhotographyBrief(
            city="杭州",
            photography_style="复古港风",
            clothing="黑色皮衣",
            shooting_date=date(2026, 10, 24),
            max_travel_minutes=40,
            max_budget_cny=300,
            crew_size=2,
            desired_scenes=["老街", "旧建筑"],
        )
        self.assertEqual(brief.max_budget_cny, 300)
        self.assertEqual(brief.model_dump(mode="json")["shooting_date"], "2026-10-24")

    def test_reject_invalid_brief(self):
        for data in [
            {"city": "", "photography_style": "日系"},
            {"city": "杭州", "photography_style": " "},
            {"city": "杭州", "photography_style": "日系", "max_travel_minutes": -1},
            {"city": "杭州", "photography_style": "日系", "max_budget_cny": -20},
            {"city": "杭州", "photography_style": "日系", "shooting_date": "下周六"},
        ]:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                PhotographyBrief(**data)

    def test_verification_queries(self):
        queries = make_verification_queries("金沙湖公园", "日系")
        self.assertEqual(len(queries), 3)
        self.assertIn("金沙湖公园", queries[0])
        with self.assertRaises(ValueError):
            make_verification_queries(" ", "复古")


if __name__ == "__main__":
    unittest.main()
