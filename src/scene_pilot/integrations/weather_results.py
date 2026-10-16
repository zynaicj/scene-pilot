"""Project only documented structured weather fields, with atomic registration."""

import uuid
from datetime import UTC, date, datetime

from scene_pilot.core.environment import CHINA_TIME, WeatherDay, WeatherEvidence
from scene_pilot.integrations.amap_results import InvalidMapResult, _text


def compact_weather(data, context, secrets=(), query=None):
    forecasts = data.get("forecasts")
    if forecasts is None and isinstance(data.get("lives"), list):
        # Current conditions cannot stand in for the requested day's forecast.
        return {
            "status": "success",
            "forecasts": [],
            "note": "接口仅返回实况天气，未取得拍摄日期的预报。",
        }
    if not isinstance(forecasts, list) or len(forecasts) > 4:
        raise InvalidMapResult()
    # Observed official MCP shape: {city, forecasts: [daily fields]}, without
    # reporttime/adcode. Keep those absent, never invent publication metadata.
    if isinstance(data.get("city"), str) and all(
        isinstance(item, dict) and "date" in item for item in forecasts
    ):
        forecasts = [
            {
                "city": data["city"],
                "adcode": data.get("adcode"),
                "reporttime": data.get("reporttime"),
                "casts": forecasts,
            }
        ]
    if len(forecasts) > 3:
        raise InvalidMapResult()
    pending = []
    for record in forecasts:
        if not isinstance(record, dict):
            raise InvalidMapResult()
        casts = record.get("casts")
        if not isinstance(casts, list) or len(casts) > 4:
            raise InvalidMapResult()
        report = _text(record.get("reporttime"), 40, secrets)
        report_time = datetime.fromisoformat(report) if report else None
        if report_time is not None and report_time.tzinfo is None:
            report_time = report_time.replace(tzinfo=CHINA_TIME)
        days, seen = [], set()
        for cast in casts:
            if not isinstance(cast, dict):
                raise InvalidMapResult()
            day = date.fromisoformat(_text(cast.get("date"), 10, secrets) or "")
            if day in seen:
                raise InvalidMapResult()
            seen.add(day)
            fields = {
                key: _text(cast.get(key), 60, secrets)
                for key in (
                    "dayweather",
                    "nightweather",
                    "daywind",
                    "nightwind",
                    "daypower",
                    "nightpower",
                )
            }
            for key in ("daytemp", "nighttemp"):
                value = cast.get(key)
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise InvalidMapResult()
                fields[key] = float(value)
            days.append(WeatherDay(date=day, **fields))
        pending.append(
            WeatherEvidence(
                evidence_id="weather_" + uuid.uuid4().hex[:16],
                retrieved_at=datetime.now(UTC),
                report_time=report_time,
                city=_text(record.get("city"), 80, secrets),
                adcode=_text(record.get("adcode"), 6, secrets),
                query_area=_text((query or {}).get("city"), 80, secrets),
                forecasts=tuple(days),
            )
        )
    # A malformed trailing record must not leave partly trusted evidence behind.
    for evidence in pending:
        context.weather[evidence.evidence_id] = evidence
    return {
        "status": "success",
        "source": "amap_mcp",
        "forecasts": [evidence.model_dump(mode="json") for evidence in pending],
        "note": "区域逐日预报；不能证明具体机位、小时级降雨或现场光线。",
    }
