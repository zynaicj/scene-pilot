"""高德 Web 服务 API 适配器；不记录或输出密钥。"""
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


class AmapError(RuntimeError):
    """高德接口调用或响应解析失败。"""


def normalize_coordinate(value: str) -> str:
    try:
        parts = [float(part.strip()) for part in value.split(",")]
        lon, lat = parts
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ValueError("坐标应为'经度,纬度'，例如 120.160000,30.280000") from None
    return f"{lon:.6f},{lat:.6f}"


def _string(value) -> str:
    return value if isinstance(value, str) else ""


class AmapClient:
    BASE_URL = "https://restapi.amap.com"

    def __init__(self, api_key: str):
        if not api_key:
            raise AmapError("请在 .env 中配置 AMAP_WEB_KEY（高德 Web 服务 Key）")
        self.api_key = api_key

    def _get(self, path: str, params: dict) -> dict:
        query = urlencode({**params, "key": self.api_key, "output": "json"})
        try:
            with urlopen(f"{self.BASE_URL}{path}?{query}", timeout=12) as response:
                data = json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise AmapError("高德请求或响应解析失败，请检查网络、接口状态及 Key 配置") from None
        if not isinstance(data, dict) or str(data.get("status")) != "1":
            info = _string(data.get("info")) if isinstance(data, dict) else "响应格式异常"
            raise AmapError(f"高德返回错误：{info or '未知错误'}")
        return data

    def search_places(self, city: str, keyword: str, limit: int = 5) -> dict:
        if not city.strip() or not keyword.strip() or not 1 <= limit <= 25:
            raise ValueError("city、keyword 不能为空，limit 必须在 1 到 25 之间")
        data = self._get("/v5/place/text", {"keywords": keyword, "region": city, "city_limit": "true", "page_size": limit, "page_num": 1})
        pois = data.get("pois", [])
        if not isinstance(pois, list):
            raise AmapError("高德地点返回结构异常")
        places = [{"poi_id": _string(p.get("id")), "name": _string(p.get("name")), "address": _string(p.get("address")), "city": _string(p.get("cityname")), "location": _string(p.get("location")), "category": _string(p.get("type"))} for p in pois[:limit] if isinstance(p, dict) and isinstance(p.get("location"), str)]
        return {"status": "success", "source": "amap", "keyword": keyword, "places": places, "count": len(places), "note": "未核验票价、营业时间、团队接待能力"}

    def driving_route(self, origin: str, destination: str) -> dict:
        start, end = normalize_coordinate(origin), normalize_coordinate(destination)
        data = self._get("/v5/direction/driving", {"origin": start, "destination": end, "strategy": 32, "show_fields": "cost"})
        route = data.get("route", {})
        paths = route.get("paths", []) if isinstance(route, dict) else []
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            raise AmapError("高德未返回可用驾车路线")
        path = paths[0]
        cost = path.get("cost", {}) if isinstance(path.get("cost"), dict) else {}
        try:
            distance = round(float(path["distance"]) / 1000, 2)
            minutes = round(float(cost["duration"]) / 60, 1)
            if not (math.isfinite(distance) and math.isfinite(minutes)) or min(distance, minutes) < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AmapError("驾车路线缺少有效的距离或耗时数据") from None
        return {"status": "success", "source": "amap", "mode": "driving", "origin": start, "destination": end, "distance_km": distance, "duration_minutes": minutes, "note": "单车单程驾车参考，不代表包车价格或保证实际路况"}
