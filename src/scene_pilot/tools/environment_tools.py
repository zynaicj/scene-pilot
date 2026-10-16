"""Bind weather/solar lookups to this request's POIs and explicit shooting date."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from scene_pilot.core.environment import (
    CHINA_TIME,
    ShootingConditions,
    WeatherAssessment,
    assess_weather,
    calculate_solar,
    weather_area_matches,
)
from scene_pilot.core.execution import current_run


def build_conditions_tool(weather_tools):
    weather_tool = next(
        (item for item in weather_tools if item.name == "maps_weather"), None
    )

    async def weather_for(facts, shooting_date, context):
        now = datetime.now(UTC)
        if shooting_date < now.astimezone(CHINA_TIME).date():
            return WeatherAssessment(
                reason="拍摄日期已过去；不查询当前预报代替历史天气。"
            )
        # Prefer the POI's city for a shared city-level forecast; a district code
        # is only used if the map did not return a city name.
        search_only = not (facts.city or facts.adcode)
        area = facts.city or facts.adcode or facts.query_city
        if not area:
            return WeatherAssessment(
                reason="地图未提供候选地点的行政区或城市，无法匹配区域天气。"
            )
        if weather_tool is None:
            return WeatherAssessment(
                reason="当前高德 MCP 未提供天气工具，天气能力暂不可用。"
            )
        result = await weather_tool.ainvoke({"city": area})
        if result.get("status") != "success":
            return WeatherAssessment(reason="天气查询失败或调用额度已耗尽。")
        records = [
            context.weather[item["evidence_id"]]
            for item in result.get("forecasts", [])
            if item.get("evidence_id") in context.weather
        ]
        # Prefer the newest publication, never an unrelated city from a multi-area result.
        matching = [
            item
            for item in records
            if weather_area_matches(
                item, facts.adcode, facts.query_city if search_only else facts.city
            )
        ]
        if not matching:
            return WeatherAssessment(
                reason="未返回匹配候选地区的逐日预报；实况不能代替预报。"
            )
        evidence = max(matching, key=lambda item: item.report_time or item.retrieved_at)
        assessment = assess_weather(
            evidence,
            shooting_date,
            facts.adcode,
            facts.query_city if search_only else facts.city,
            now,
        )
        if search_only and assessment.status != "unknown":
            assessment = assessment.model_copy(
                update={
                    "status": "reference",
                    "area_basis": "search_city",
                    "reason": "仅按地点搜索城市提供参考；POI 行政归属未核实。"
                    + assessment.reason,
                    "scope": "搜索城市逐日预报，不代表此候选地点或具体机位的天气。",
                }
            )
        return assessment

    @tool
    async def inspect_shooting_conditions(
        evidence_ids: Annotated[list[str], Field(min_length=1, max_length=3)],
    ) -> dict:
        """按已保存需求中的明确日期，查询最多三处本次 POI 的区域天气并计算太阳时段。只传证据编号。"""
        context = current_run()
        if len(set(evidence_ids)) != len(evidence_ids) or any(
            identity not in context.evidence for identity in evidence_ids
        ):
            return {"status": "error", "error_code": "unknown_or_duplicate_poi"}
        if context.brief is None:
            return {"status": "error", "error_code": "prepare_brief_first"}
        # Serialize this local orchestrator too: simultaneous calls cannot bypass
        # its three-candidate limit or duplicate calculations.
        if "conditions_lock" not in context.cache:
            context.cache["conditions_lock"] = asyncio.Lock()
        async with context.cache["conditions_lock"]:
            if len(set(context.conditions) | set(evidence_ids)) > 3:
                return {"status": "error", "error_code": "condition_candidate_limit"}
            context.recommendation = None
            for identity in evidence_ids:
                if identity in context.conditions:
                    continue
                facts = context.evidence[identity]
                shooting_date = context.brief.shooting_date
                if shooting_date is None:
                    context.conditions[identity] = ShootingConditions(
                        poi_evidence_id=identity,
                        weather=WeatherAssessment(
                            reason="缺少明确拍摄日期；请提供 YYYY-MM-DD。"
                        ),
                    )
                    continue
                solar = calculate_solar(facts.location, shooting_date)
                context.metrics["solar_calculations"] += 1
                # Save local results before awaiting the network, so cancellation
                # can still deliver the solar estimate with unknown weather.
                report = ShootingConditions(
                    poi_evidence_id=identity,
                    shooting_date=shooting_date,
                    solar=solar,
                )
                context.conditions[identity] = report
                weather = await weather_for(facts, shooting_date, context)
                context.conditions[identity] = report.model_copy(
                    update={"weather": weather}
                )
        return {
            "status": "success",
            "conditions": [
                context.conditions[key].model_dump(mode="json") for key in evidence_ids
            ],
        }

    return inspect_shooting_conditions
