"""Local business checks use confirmed requirements and bound map evidence only."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from scene_pilot.core.confirmation import coordinates


class TravelTimeCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["meets", "not_met", "unknown"]
    reason: str
    limit_minutes: int | None = None
    estimated_minutes: float | None = None
    route_evidence_id: str | None = None


def route_matches_confirmation(route, confirmed, departure) -> bool:
    if not confirmed or not departure:
        return False
    brief = confirmed.brief
    try:
        return (
            brief.transport_mode == "驾车"
            and departure.query_address == brief.departure
            and departure.query_city == brief.city
            and route.brief_confirmation_id == confirmed.confirmation_id
            and route.origin_evidence_id == departure.evidence_id
            and coordinates(route.query_origin) == coordinates(departure.location)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def check_travel_time(confirmed, departure, route) -> TravelTimeCheck:
    reason = None
    brief = confirmed.brief if confirmed else None
    limit = brief.max_travel_minutes if brief else None
    if brief is None:
        reason = "需求尚未经过用户确认，不能判断是否符合。"
    elif limit is None:
        reason = "用户未设置单程时长上限。"
    elif brief.transport_mode != "驾车":
        reason = "交通方式未明确为驾车；当前只有驾车路线能力。"
    elif not departure:
        reason = "出发点的地图位置尚未由用户确认。"
    elif route is None or not route_matches_confirmation(route, confirmed, departure):
        reason = "没有与本次已确认出发点和驾车方式绑定的路线证据。"
    if reason:
        return TravelTimeCheck(status="unknown", reason=reason, limit_minutes=limit)
    return TravelTimeCheck(
        status="meets" if route.duration_s <= limit * 60 else "not_met",
        reason="按查询时的单程驾车估算比较，不保证拍摄当天路况。",
        limit_minutes=limit,
        estimated_minutes=route.duration_s / 60,
        route_evidence_id=route.evidence_id,
    )
