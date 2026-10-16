"""Weather evidence and local solar estimates; neither verifies a shooting site."""

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from astral import Observer, SunDirection
from astral.sun import sunrise, sunset, time_at_elevation
from pydantic import BaseModel, ConfigDict, Field

CHINA_TIME = ZoneInfo("Asia/Shanghai")


class EnvironmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class WeatherDay(EnvironmentModel):
    date: date
    dayweather: str = Field(min_length=1, max_length=60)
    nightweather: str = Field(min_length=1, max_length=60)
    daytemp: float = Field(ge=-90, le=65)
    nighttemp: float = Field(ge=-90, le=65)
    daywind: str | None = None
    nightwind: str | None = None
    daypower: str | None = None
    nightpower: str | None = None


class WeatherEvidence(EnvironmentModel):
    evidence_id: str
    source: Literal["amap_mcp"] = "amap_mcp"
    tool: Literal["maps_weather"] = "maps_weather"
    retrieved_at: datetime
    report_time: datetime | None = None
    city: str
    adcode: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    query_area: str | None = None
    forecasts: tuple[WeatherDay, ...] = Field(max_length=4)


class WeatherAssessment(EnvironmentModel):
    status: Literal["available", "reference", "unknown"] = "unknown"
    reason: str = "尚未查询拍摄日期的天气。"
    evidence_id: str | None = None
    city: str | None = None
    report_time: datetime | None = None
    retrieved_at: datetime | None = None
    forecast: WeatherDay | None = None
    area_basis: Literal["poi_metadata", "search_city", "unknown"] = "unknown"
    scope: str = "行政区域逐日预报，不代表具体机位或拍摄时刻的天气。"


class SolarEstimate(EnvironmentModel):
    source: Literal["astral_local_calculation"] = "astral_local_calculation"
    status: Literal["available", "partial", "unknown"] = "unknown"
    date: date
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    location: str | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    morning_golden: tuple[datetime, datetime] | None = None
    evening_golden: tuple[datetime, datetime] | None = None
    note: str = (
        "国内选景按北京时间、海拔 0 米估算；直接使用高德坐标近似计算，非测绘精度。"
        "黄金时段定义为太阳高度 -4° 至 +6°；未考虑云层、地形、建筑或树木遮挡。"
    )


class ShootingConditions(EnvironmentModel):
    poi_evidence_id: str
    shooting_date: date | None = None
    weather: WeatherAssessment = Field(default_factory=WeatherAssessment)
    solar: SolarEstimate | None = None
    note: str = "日期来自本次需求提取；请核对日期。无明确日期时不猜测天气或光照时间。"


def _golden_hour_local(observer, date, tzinfo, direction):
    # Astral 3.2 golden_hour does not correct UTC transit dates to the requested
    # local date (unlike sunrise/sunset). Correct each endpoint independently.
    endpoints = []
    for elevation in (-4, 6):
        value = time_at_elevation(observer, elevation, date, direction, tzinfo)
        if value.date() != date:
            adjusted = date + (date - value.date())
            value = time_at_elevation(observer, elevation, adjusted, direction, tzinfo)
        if value.date() != date:
            raise ValueError("no_event_on_requested_local_date")
        endpoints.append(value)
    return tuple(sorted(endpoints))


def calculate_solar(location: str | None, shooting_date: date) -> SolarEstimate:
    """Calculate each event independently so polar days retain a safe partial result."""
    base = {"date": shooting_date, "location": location}
    if location is None:
        return SolarEstimate(**base)
    try:
        longitude, latitude = map(float, location.split(","))
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("invalid_coordinates")
    except (ValueError, TypeError):
        return SolarEstimate(**base)
    observer = Observer(latitude=latitude, longitude=longitude, elevation=0)
    events = {}
    for name, calculation, options in (
        ("sunrise", sunrise, {}),
        ("sunset", sunset, {}),
        ("morning_golden", _golden_hour_local, {"direction": SunDirection.RISING}),
        ("evening_golden", _golden_hour_local, {"direction": SunDirection.SETTING}),
    ):
        try:
            events[name] = calculation(
                observer, date=shooting_date, tzinfo=CHINA_TIME, **options
            )
        except (ValueError, OverflowError):
            # A sun that never crosses an event's elevation has no such event.
            events[name] = None
    available = sum(value is not None for value in events.values())
    status = "available" if available == 4 else "partial" if available else "unknown"
    return SolarEstimate(**base, status=status, **events)


def weather_area_matches(evidence, adcode, city):
    if evidence.adcode and adcode and evidence.adcode == adcode:
        return True
    if (
        evidence.adcode
        and adcode
        and not (evidence.adcode.endswith("00") and evidence.adcode[:4] == adcode[:4])
    ):
        return False
    # The official MCP may omit adcode. In that case require both our actual
    # query and the returned city name to agree with the POI's map metadata.
    return (
        bool(city)
        and evidence.city.removesuffix("市") == city.removesuffix("市")
        and (
            evidence.query_area == city
            or (evidence.adcode is None and evidence.query_area == adcode)
        )
    )


def assess_weather(evidence, shooting_date, adcode, city, now):
    """Match area, publication freshness and exact date; never extrapolate forecasts."""
    unknown = WeatherAssessment
    matches = weather_area_matches(evidence, adcode, city)
    if not matches:
        return unknown(reason="天气返回地区与候选地点不一致，不能套用。")
    if evidence.report_time is not None:
        age = now - evidence.report_time
        if age < -timedelta(minutes=10) or age > timedelta(hours=24):
            return unknown(reason="预报发布时间过旧或异常，不能作为本次拍摄预报。")
    if shooting_date < now.astimezone(CHINA_TIME).date():
        return unknown(reason="拍摄日期已过去；当前预报不能用于查询历史天气。")
    forecast = next(
        (day for day in evidence.forecasts if day.date == shooting_date), None
    )
    if forecast is None:
        return unknown(reason="返回的预报不覆盖拍摄日期；请临近拍摄时重新查询。")
    return WeatherAssessment(
        status="available" if evidence.report_time else "reference",
        reason=(
            "已匹配地区与拍摄日期；预报仍可能变化。"
            if evidence.report_time
            else "接口未返回发布时间，时效性未知；仅供参考，临近拍摄需复查。"
        ),
        evidence_id=evidence.evidence_id,
        city=evidence.city,
        report_time=evidence.report_time,
        retrieved_at=evidence.retrieved_at,
        forecast=forecast,
        area_basis="poi_metadata",
    )


def render_conditions(conditions: ShootingConditions) -> list[str]:
    weather = conditions.weather
    lines = [
        f"拍摄日期：{conditions.shooting_date or '未明确'}（请核对需求提取结果）。"
    ]
    if weather.status in {"available", "reference"} and weather.forecast:
        day = weather.forecast
        lines.append(
            f"{'搜索城市参考天气' if weather.area_basis == 'search_city' else '区域天气预报'}"
            f"（{weather.city}）：白天 {day.dayweather} {day.daytemp:g}℃；"
            f"夜间 {day.nightweather} {day.nighttemp:g}℃。"
            f"高德 maps_weather / {weather.evidence_id}；"
            f"发布时间 {weather.report_time.isoformat() if weather.report_time else '未知'}；"
            f"查询时间 {weather.retrieved_at.isoformat()}。"
        )
        lines.append(weather.reason)
        lines.append(weather.scope)
    else:
        lines.append("天气未知：" + weather.reason)
    solar = conditions.solar
    if solar:
        lines.append(f"天文估算（{solar.timezone}）：")
        for label, value in (("日出", solar.sunrise), ("日落", solar.sunset)):
            lines.append(
                f"- {label}：{value.strftime('%H:%M') if value else '无法计算或当日无此事件'}"
            )
        for label, window in (
            ("晨间黄金时段", solar.morning_golden),
            ("傍晚黄金时段", solar.evening_golden),
        ):
            text = (
                "—".join(value.strftime("%H:%M") for value in window)
                if window
                else "未知或当日不存在"
            )
            lines.append(f"- {label}：{text}")
        lines.append(solar.note)
    else:
        lines.append("光照时段未知：需明确日期和候选地点坐标，并执行条件查询。")
    return lines
