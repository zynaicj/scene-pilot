"""Evidence-backed output. Only the adapter supplies map facts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scene_pilot.core.photography import PhotographyBrief, make_verification_queries
from scene_pilot.core.environment import ShootingConditions, render_conditions
from scene_pilot.core.confirmation import DepartureEvidence, coordinates
from scene_pilot.core.constraints import (
    TravelTimeCheck,
    check_travel_time,
    route_matches_confirmation,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MapEvidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    source: Literal["amap_mcp"] = "amap_mcp"
    tool: Literal["maps_text_search", "maps_around_search", "maps_search_detail"]
    retrieved_at: datetime
    poi_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    location: str | None = None
    city: str | None = Field(default=None, max_length=80)
    adcode: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    query_city: str | None = Field(
        default=None,
        max_length=80,
        description="地点搜索的城市参数，不代表服务返回或已确认的 POI 行政归属",
    )

    @model_validator(mode="after")
    def identity_and_coordinates(self):
        if not self.poi_id and not self.location:
            raise ValueError("POI identity requires an ID or coordinates")
        if self.location is not None:
            try:
                longitude, latitude = map(float, self.location.split(","))
            except (ValueError, TypeError):
                raise ValueError("Invalid coordinates") from None
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                raise ValueError("Coordinates out of range")
        return self


class UserStatement(StrictModel):
    source: Literal["user_quote"] = "user_quote"
    quote: str = Field(min_length=1, max_length=1000)


class PhotographyAdvice(StrictModel):
    source: Literal["ai_inference"] = "ai_inference"
    text: str = Field(min_length=1, max_length=600)


class DrivingRouteEvidence(StrictModel):
    evidence_id: str
    source: Literal["amap_mcp"] = "amap_mcp"
    tool: Literal["maps_direction_driving"] = "maps_direction_driving"
    retrieved_at: datetime
    query_origin: str
    query_destination: str
    distance_m: float = Field(ge=0, allow_inf_nan=False)
    duration_s: float = Field(ge=0, allow_inf_nan=False)
    brief_confirmation_id: str | None = None
    origin_evidence_id: str | None = None

    @field_validator("query_origin", "query_destination")
    @classmethod
    def valid_coordinates(cls, value):
        coordinates(value)
        return value


UNKNOWN_FIELDS = (
    "ticket_price",
    "opening_hours",
    "rating",
    "commercial_permission",
    "camera_position",
    "seasonal_vegetation",
    "crowd_level",
    "visual_appearance",
)


class UnknownVenueInfo(StrictModel):
    status: Literal["unverified"] = "unverified"
    fields: tuple[str, ...] = UNKNOWN_FIELDS
    reason: str = "当前适配器没有可验证的对应来源；需查询官方场地信息与近期实拍。"

    @model_validator(mode="after")
    def cannot_drop_unknowns(self):
        if self.fields != UNKNOWN_FIELDS:
            raise ValueError("Unknown venue fields cannot be marked verified")
        return self


class CandidateDraft(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=80)
    advice: list[PhotographyAdvice] = Field(default_factory=list, max_length=3)
    route_evidence_id: str | None = None


class CandidateRecommendation(StrictModel):
    facts: MapEvidence
    advice: list[PhotographyAdvice] = Field(default_factory=list, max_length=3)
    unknown: UnknownVenueInfo = Field(default_factory=UnknownVenueInfo)
    verification_queries: list[str]
    driving_route: DrivingRouteEvidence | None = None
    conditions: ShootingConditions | None = None
    travel_time: TravelTimeCheck


class PhotographyRecommendation(StrictModel):
    user_statements: list[UserStatement] = Field(default_factory=list, max_length=12)
    candidates: list[CandidateRecommendation] = Field(
        default_factory=list, max_length=3
    )
    status: Literal["completed", "partial", "no_verified_candidates"]
    confirmed_requirements: PhotographyBrief | None = None
    confirmation_id: str | None = None
    confirmed_departure: DepartureEvidence | None = None


def assemble_recommendation(
    request,
    statements,
    drafts,
    evidence,
    routes=None,
    conditions=None,
    *,
    confirmed_brief=None,
    departure=None,
):
    """Check exact user quotations and resolve facts against this run's registry."""
    if any(item.quote not in request for item in statements):
        raise ValueError("User statement is not an exact quote")
    candidates = []
    seen = set()
    for draft in drafts:
        facts = evidence.get(draft.evidence_id)
        if facts is None:
            raise ValueError("Unknown evidence ID")
        identity = facts.poi_id or (facts.name, facts.location)
        if identity in seen:
            raise ValueError("Duplicate candidate")
        seen.add(identity)
        route = None
        if draft.route_evidence_id:
            route = (routes or {}).get(draft.route_evidence_id)
            if route is None or facts.location is None:
                raise ValueError("Unknown route evidence")
            if tuple(map(float, route.query_destination.split(","))) != tuple(
                map(float, facts.location.split(","))
            ):
                raise ValueError("Route destination does not match POI")
            if confirmed_brief and not route_matches_confirmation(
                route, confirmed_brief, departure
            ):
                raise ValueError(
                    "Route origin or transport does not match confirmation"
                )
        elif confirmed_brief and facts.location:
            # Do not let omission of a route ID hide an already known violation.
            route = next(
                (
                    item
                    for item in (routes or {}).values()
                    if coordinates(item.query_destination)
                    == coordinates(facts.location)
                    and route_matches_confirmation(item, confirmed_brief, departure)
                ),
                None,
            )
        candidates.append(
            CandidateRecommendation(
                facts=facts,
                advice=draft.advice,
                verification_queries=make_verification_queries(facts.name, "人像"),
                driving_route=route,
                conditions=(conditions or {}).get(facts.evidence_id),
                travel_time=check_travel_time(confirmed_brief, departure, route),
            )
        )
    return PhotographyRecommendation(
        user_statements=statements,
        candidates=candidates,
        status="completed" if candidates else "no_verified_candidates",
        confirmed_requirements=confirmed_brief.brief if confirmed_brief else None,
        confirmation_id=confirmed_brief.confirmation_id if confirmed_brief else None,
        confirmed_departure=departure,
    )


def render_recommendation(plan: PhotographyRecommendation) -> str:
    lines = ["用户原话："]
    lines.extend(f"- {item.quote}" for item in plan.user_statements)
    if plan.confirmed_requirements:
        brief = plan.confirmed_requirements
        lines.append(
            f"用户已确认需求：{brief.city} / {brief.photography_style}；"
            f"出发地 {brief.departure or '未填写'}；方式 {brief.transport_mode or '未填写'}；"
            f"单程上限 {brief.max_travel_minutes if brief.max_travel_minutes is not None else '未设置'} 分钟。"
        )
        if brief.max_budget_cny is not None:
            lines.append(
                f"总预算上限 {brief.max_budget_cny:g} 元：信息不足，尚无可靠的完整费用证据。"
            )
    else:
        lines.append("需求未经用户确认；交通时长等约束只能标为信息不足。")
    if plan.confirmed_departure:
        origin = plan.confirmed_departure
        lines.append(
            f"用户选择的地图出发位置：{origin.formatted_address or origin.query_address}；"
            f"坐标 {origin.location}；来源 {origin.evidence_id}。"
        )
    if not plan.candidates:
        lines.append("本次没有取得可校验的候选地点。请明确城市和风格，或稍后重试。")
    for item in plan.candidates:
        facts = item.facts
        lines.extend(
            [
                f"\n{facts.name}",
                (
                    f"地图返回信息（非现场核验）：地址 {facts.address or '未知'}；"
                    f"坐标 {facts.location or '未知'}；POI ID {facts.poi_id or '未知'}。"
                ),
                f"来源：{facts.tool} / {facts.evidence_id} / {facts.retrieved_at.isoformat()}",
                "摄影建议/AI 推断（未经现场核验）：",
            ]
        )
        lines.extend(f"- {advice.text}" for advice in item.advice)
        if item.driving_route:
            route = item.driving_route
            lines.append(
                f"地图驾车估算：{route.distance_m / 1000:g} 公里，"
                f"{route.duration_s / 60:g} 分钟；查询起点坐标 {route.query_origin}。"
                f"来源 {route.evidence_id}；不保证拍摄当天路况。"
            )
        check = item.travel_time
        label = {"meets": "符合", "not_met": "不符合", "unknown": "信息不足"}[
            check.status
        ]
        comparison = (
            f"估算 {check.estimated_minutes:g} 分钟 / 上限 {check.limit_minutes} 分钟。"
            if check.estimated_minutes is not None
            else ""
        )
        lines.append(f"单程时长校验：{label}。{comparison}{check.reason}")
        lines.append(
            "待人工确认：门票、开放时间、评分、商业拍摄许可、机位、植被、人流和视觉效果。"
        )
        if item.conditions:
            lines.extend(render_conditions(item.conditions))
        else:
            lines.append("天气与太阳时段：尚未取得本次候选地点的条件报告。")
        lines.append("小红书/抖音人工搜索：" + "；".join(item.verification_queries))
    return "\n".join(lines)
