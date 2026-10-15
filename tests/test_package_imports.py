"""迁移后的包及命令行入口都可导入。"""
import unittest


class PackageImportTests(unittest.TestCase):
    def test_new_imports(self):
        from scene_pilot.agent import build_scene_pilot_agent
        from scene_pilot.core.photography import PhotographyBrief
        from scene_pilot.main import main
        from scene_pilot.integrations.amap_mcp import select_amap_tools
        self.assertTrue(callable(build_scene_pilot_agent))
        self.assertTrue(callable(main))
        self.assertTrue(callable(select_amap_tools))
        self.assertEqual(PhotographyBrief(city="杭州", photography_style="日系").city, "杭州")


if __name__ == "__main__":
    unittest.main()
