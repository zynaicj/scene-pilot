"""真实调用本地 LangChain 工具验证摄影需求与场景核验。"""
import unittest
from scene_pilot.tools.photography_tools import prepare_photography_brief, recommend_scene_verification


class PhotographyToolTests(unittest.TestCase):
    def test_brief_tool(self):
        result = prepare_photography_brief.invoke({
            "city": "杭州",
            "photography_style": "日系清新",
            "clothing": "白色长裙",
            "desired_scenes": ["树林", "草地"],
            "max_budget_cny": 300,
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["brief"]["city"], "杭州")
        self.assertNotIn("shooting_date", result["brief"])

    def test_verification_tool(self):
        result = recommend_scene_verification.invoke({
            "place_name": "某真实候选地",
            "photography_style": "日系",
        })
        self.assertEqual(result["verification_state"], "unverified")
        self.assertEqual(len(result["queries"]), 3)


if __name__ == "__main__":
    unittest.main()
