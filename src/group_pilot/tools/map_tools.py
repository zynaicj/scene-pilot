"""仅提供真实地图查询，不生成未经核实的价格数据。"""
from langchain_core.tools import tool
from group_pilot.core.config import get_settings
from group_pilot.integrations.amap import AmapClient, AmapError


def _client() -> AmapClient:
    return AmapClient(get_settings().amap_web_key)


@tool
def search_destinations(city: str, keyword: str) -> dict:
    """搜索指定城市的真实候选地点，返回名称、地址、POI ID 和经纬度；不含已核实门票价格。"""
    try:
        return _client().search_places(city, keyword)
    except (AmapError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@tool
def locate_departure(city: str, address: str) -> dict:
    """按城市和出发地名称检索高德地点坐标；有歧义时需向用户确认。"""
    try:
        return _client().search_places(city, address, limit=3)
    except (AmapError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


@tool
def query_driving_route(origin: str, destination: str) -> dict:
    """查询驾车路线。origin 和 destination 必须为经度,纬度字符串，例如 120.16,30.28。"""
    try:
        return _client().driving_route(origin, destination)
    except (AmapError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}
