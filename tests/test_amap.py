"""真实 API 适配器的纯本地单元测试，不调用付费或实时接口。"""
import io
import json
import unittest
from unittest.mock import patch
from group_pilot.integrations.amap import AmapClient, AmapError, normalize_coordinate


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def response(data: dict):
    return FakeResponse(json.dumps(data).encode("utf-8"))


class TestAmap(unittest.TestCase):
    def test_coordinate(self):
        self.assertEqual(normalize_coordinate("120.16,30.28"), "120.160000,30.280000")

    def test_invalid_coordinates(self):
        for value in ("abc,30", "120", "200,30", "120,100", "nan,30", "120,30,20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_coordinate(value)

    @patch("group_pilot.integrations.amap.urlopen")
    def test_place_search(self, mock_urlopen):
        mock_urlopen.return_value = response({"status": "1", "pois": [{"id": "P01", "name": "示例公园", "location": "120.16,30.28", "address": "测试地址"}]})
        result = AmapClient("test-key").search_places("杭州", "公园")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["places"][0]["poi_id"], "P01")
        self.assertNotIn("test-key", str(result))

    @patch("group_pilot.integrations.amap.urlopen")
    def test_route(self, mock_urlopen):
        mock_urlopen.return_value = response({"status": "1", "route": {"paths": [{"distance": "20000", "cost": {"duration": "1800"}}]}})
        result = AmapClient("test-key").driving_route("120.16,30.28", "120.22,30.30")
        self.assertEqual(result["distance_km"], 20)
        self.assertEqual(result["duration_minutes"], 30)

    @patch("group_pilot.integrations.amap.urlopen")
    def test_api_error_does_not_expose_key(self, mock_urlopen):
        mock_urlopen.return_value = response({"status": "0", "info": "INVALID_USER_KEY"})
        with self.assertRaises(AmapError) as err:
            AmapClient("private-api-key").search_places("杭州", "公园")
        self.assertNotIn("private-api-key", str(err.exception))

    @patch("group_pilot.integrations.amap.urlopen")
    def test_missing_route_duration(self, mock_urlopen):
        mock_urlopen.return_value = response({"status": "1", "route": {"paths": [{"distance": "20000"}]}})
        with self.assertRaises(AmapError):
            AmapClient("test-key").driving_route("120.16,30.28", "120.22,30.30")


if __name__ == "__main__":
    unittest.main()
