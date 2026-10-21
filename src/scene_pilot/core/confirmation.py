"""Host-side confirmation snapshots; these functions are never model tools."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scene_pilot.core.photography import PhotographyBrief


def coordinates(value: str) -> tuple[float, float]:
    longitude, latitude = map(float, value.split(","))
    if not (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    ):
        raise ValueError("invalid_coordinates")
    return longitude, latitude


class BriefDraft(PhotographyBrief):
    """Missing core fields are allowed only before human confirmation."""

    city: str | None = Field(default=None, max_length=60)
    photography_style: str | None = Field(default=None, max_length=100)


@dataclass(frozen=True)
class PreparationUsage:
    model_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_missing: int = 0


@dataclass(frozen=True, repr=False)
class ConfirmedBrief:
    request: str
    _brief_json: str
    confirmation_id: str = field(default_factory=lambda: uuid4().hex)
    confirmed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    preparation_usage: PreparationUsage = field(default_factory=PreparationUsage)

    @property
    def brief(self) -> PhotographyBrief:
        # A fresh value also prevents mutation of list fields inside the snapshot.
        return PhotographyBrief.model_validate_json(self._brief_json)


def confirm_brief(
    request: str,
    brief: PhotographyBrief,
    *,
    accepted: bool,
    preparation_usage: PreparationUsage | None = None,
) -> ConfirmedBrief:
    """Call only after a real user's explicit acceptance in the host UI/API."""
    if accepted is not True:
        raise ValueError("confirmation_required")
    validated = PhotographyBrief.model_validate(brief.model_dump())
    return ConfirmedBrief(
        request=request,
        _brief_json=validated.model_dump_json(),
        preparation_usage=preparation_usage or PreparationUsage(),
    )


class DepartureEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    source: Literal["amap_mcp"] = "amap_mcp"
    tool: Literal["maps_geo"] = "maps_geo"
    retrieved_at: datetime
    query_address: str = Field(min_length=1, max_length=120)
    query_city: str = Field(min_length=1, max_length=60)
    location: str
    formatted_address: str | None = None

    @field_validator("location")
    @classmethod
    def valid_coordinates(cls, value):
        coordinates(value)
        return value


async def resolve_departure(context, geocode_tool, select_origin) -> float:
    """Resolve the exact confirmed address, then ask the host UI to select it.

    Returns human waiting time, excluded from the active execution deadline.
    All service attempts still use this request's normal map budget and cache.
    """
    import asyncio
    import time

    confirmed = context.confirmed_brief
    if not confirmed or geocode_tool is None or select_origin is None:
        return 0.0
    brief = confirmed.brief
    if brief.transport_mode != "驾车" or not brief.departure:
        return 0.0
    async with asyncio.timeout(context.limits.timeout_seconds):
        result = await geocode_tool.ainvoke(
            {"address": brief.departure, "city": brief.city}
        )
    if result.get("status") != "success":
        return 0.0
    options = tuple(
        context.geocodes[item["evidence_id"]]
        for item in result.get("geocodes", [])
        if item.get("evidence_id") in context.geocodes
    )
    if not options:
        return 0.0
    started = time.perf_counter()
    selected = await select_origin(options)
    waiting = time.perf_counter() - started
    # The selector returns only an ID; it cannot submit replacement coordinates.
    context.departure = next(
        (item for item in options if item.evidence_id == selected), None
    )
    return waiting
