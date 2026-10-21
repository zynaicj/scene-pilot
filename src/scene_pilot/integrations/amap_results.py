"""Conservative MCP result projection; no raw prose is treated as evidence."""

import json
import math
import re
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError

from scene_pilot.core.recommendation import MapEvidence
from scene_pilot.core.confirmation import DepartureEvidence


class InvalidMapResult(ValueError):
    pass


def safe_error(code: str) -> dict:
    return {
        "status": "error",
        "source": "amap_mcp",
        "error_code": code,
        "retryable": False,
        "message": "本次地图查询未完成；请使用已有证据或明确报告未知，不得编造地点。",
    }


def decode_result(raw):
    """Accept structured JSON or MCP text blocks. Reject oversized/malformed data."""
    if isinstance(raw, str):
        if len(raw) > 250000:
            raise InvalidMapResult()
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raise InvalidMapResult() from None
    if isinstance(raw, list):
        if len(raw) > 100:
            raise InvalidMapResult()
        decoded = []
        for block in raw:
            if not isinstance(block, dict) or block.get("type") != "text":
                raise InvalidMapResult()
            decoded.append(decode_result(block.get("text")))
        if len(decoded) == 1:
            return decoded[0]
        if decoded and all(isinstance(item.get("pois"), list) for item in decoded):
            return {"pois": [poi for item in decoded for poi in item["pois"]]}
        raise InvalidMapResult()
    if not isinstance(raw, dict):
        raise InvalidMapResult()
    if raw.get("isError") or raw.get("status") in ("0", 0, "error", "failed"):
        raise InvalidMapResult()
    if "structured_content" in raw:
        return decode_result(raw["structured_content"])
    if "data" in raw and isinstance(raw["data"], dict):
        return decode_result(raw["data"])
    return raw


def _text(value, limit: int, secrets: tuple[str, ...]):
    if value in (None, "", []):
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise InvalidMapResult()
    if any(secret and secret in value for secret in secrets):
        raise InvalidMapResult()
    if re.search(
        r"https?://|api.?key|authorization|bearer\s|key=", value, re.IGNORECASE
    ):
        raise InvalidMapResult()
    return value.strip() or None


def _number(value):
    if isinstance(value, bool):
        raise InvalidMapResult()
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise InvalidMapResult() from None
    if not math.isfinite(number) or number < 0:
        raise InvalidMapResult()
    return number


def compact_result(raw, tool_name, context, secrets=(), query=None):
    data = decode_result(raw)
    if tool_name in {"maps_text_search", "maps_around_search", "maps_search_detail"}:
        pois = data.get("pois")
        if pois is None and tool_name == "maps_search_detail" and "name" in data:
            pois = [data]
        if not isinstance(pois, list) or len(pois) > 1000:
            raise InvalidMapResult()
        result, seen, omitted = [], set(), 0
        for poi in pois:
            if not isinstance(poi, dict):
                omitted += 1
                continue
            try:
                fields = {
                    "poi_id": _text(poi.get("id"), 80, secrets),
                    "name": _text(poi.get("name"), 200, secrets),
                    "address": _text(poi.get("address"), 500, secrets),
                    "location": _text(poi.get("location"), 80, secrets),
                    "city": _text(poi.get("cityname"), 80, secrets),
                    "adcode": _text(poi.get("adcode"), 6, secrets),
                }
                fields["query_city"] = (
                    _text((query or {}).get("city"), 80, secrets)
                    if tool_name in {"maps_text_search", "maps_around_search"}
                    else None
                )
                if tool_name == "maps_search_detail" and fields["poi_id"]:
                    query_cities = {
                        entry.query_city
                        for entry in context.evidence.values()
                        if entry.poi_id == fields["poi_id"] and entry.query_city
                    }
                    if len(query_cities) == 1:
                        fields["query_city"] = next(iter(query_cities))
                candidate = MapEvidence(
                    evidence_id="poi_" + uuid.uuid4().hex[:16],
                    tool=tool_name,
                    retrieved_at=datetime.now(UTC),
                    **fields,
                )
            except (InvalidMapResult, ValidationError):
                omitted += 1
                continue
            identity = candidate.poi_id or (candidate.name, candidate.location)
            if identity in seen:
                omitted += 1
                continue
            seen.add(identity)
            if len(result) >= context.limits.max_pois:
                omitted += 1
                continue
            # Reuse an ID only when all projected facts agree. Changed facts get
            # a fresh immutable evidence record, so old references stay stable.
            existing = next(
                (
                    entry
                    for entry in context.evidence.values()
                    if all(
                        getattr(entry, key) == value for key, value in fields.items()
                    )
                ),
                None,
            )
            candidate = existing or candidate
            context.evidence[candidate.evidence_id] = candidate
            result.append(candidate.model_dump(mode="json"))
        if pois and not result:
            raise InvalidMapResult()
        return {
            "status": "success",
            "source": "amap_mcp",
            "pois": result,
            "omitted_count": omitted,
            "note": "仅保留完整的地点基本事实；未保留的条目计入 omitted_count。费用、开放时间、许可和视觉效果未核验。",
        }
    if tool_name == "maps_geo":
        geocodes = data.get("geocodes")
        if not isinstance(geocodes, list):
            raise InvalidMapResult()
        locations = []
        for item in geocodes[:3]:
            if not isinstance(item, dict):
                raise InvalidMapResult()
            location = _text(item.get("location"), 80, secrets)
            # Reuse coordinate validation without treating a geocode as a POI.
            MapEvidence(
                evidence_id="geo",
                tool="maps_text_search",
                retrieved_at=datetime.now(UTC),
                name="geocode",
                location=location,
            )
            projected = {
                "location": location,
                "formatted_address": _text(item.get("formatted_address"), 500, secrets),
            }
            address = _text((query or {}).get("address"), 120, secrets)
            city = _text((query or {}).get("city"), 60, secrets)
            if address and city:
                evidence = DepartureEvidence(
                    evidence_id="origin_" + uuid.uuid4().hex[:16],
                    retrieved_at=datetime.now(UTC),
                    query_address=address,
                    query_city=city,
                    **projected,
                )
                context.geocodes[evidence.evidence_id] = evidence
                projected["evidence_id"] = evidence.evidence_id
            locations.append(projected)
        return {
            "status": "success",
            "source": "amap_mcp",
            "geocodes": locations,
            "omitted_count": max(0, len(geocodes) - 3),
        }
    if tool_name == "maps_direction_driving":
        route = data.get("route", data)
        paths = route.get("paths") if isinstance(route, dict) else None
        if not isinstance(paths, list):
            raise InvalidMapResult()
        compact_paths = []
        for path in paths[:3]:
            if not isinstance(path, dict):
                raise InvalidMapResult()
            duration = path.get("duration")
            if duration is None and isinstance(path.get("cost"), dict):
                duration = path["cost"].get("duration")
            compact_paths.append(
                {
                    "distance_m": _number(path.get("distance")),
                    "duration_s": _number(duration),
                }
            )
        return {
            "status": "success",
            "source": "amap_mcp",
            "paths": compact_paths,
            "omitted_count": max(0, len(paths) - 3),
            "note": "查询时的驾车估算，不保证拍摄当天路况。",
        }
    if tool_name == "maps_weather":
        from scene_pilot.integrations.weather_results import compact_weather

        return compact_weather(data, context, secrets, query)
    raise InvalidMapResult()
