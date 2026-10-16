"""Request-scoped cache with single-flight execution and bounded projection."""

import asyncio
import copy
import json
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from langchain_core.tools import StructuredTool

from scene_pilot.core.execution import current_run
from scene_pilot.core.recommendation import DrivingRouteEvidence
from scene_pilot.integrations.amap_results import compact_result, safe_error


def govern_amap_tools(tools, api_key: str):
    async def invoke(tool, kwargs):
        context = current_run()
        context.metrics["map_requests"] += 1
        key = (tool.name, json.dumps(kwargs, sort_keys=True, ensure_ascii=False))
        # Contexts are never shared between user requests; a single-flight lock
        # prevents concurrent duplicate cache misses from spending two calls.
        if "lock" not in context.cache:
            context.cache["lock"] = asyncio.Lock()
        async with context.cache["lock"]:
            cached = context.cache.get(key)
            if (
                cached
                and time.monotonic() - cached[0] < context.limits.cache_ttl_seconds
            ):
                context.metrics["cache_hits"] += 1
                return copy.deepcopy(cached[1])
            started = time.perf_counter()
            raw = await tool.ainvoke(kwargs)
            context.metrics["map_elapsed_ms"] += round(
                (time.perf_counter() - started) * 1000
            )
            try:
                raw_size = len(json.dumps(raw, ensure_ascii=False))
                context.metrics["raw_result_chars"] += raw_size
                if raw_size > 250000:
                    raise ValueError("oversized_result")
                # Structured provider artifacts may also contain status=error.
                # Never echo arbitrary provider-controlled error codes.
                if isinstance(raw, dict) and raw.get("status") == "error":
                    code = raw.get("error_code")
                    if code not in {
                        "map_budget_exceeded",
                        "transport_unavailable",
                        "tool_execution_failed",
                    }:
                        code = "invalid_tool_response"
                    return safe_error(code)
                result = compact_result(
                    raw,
                    tool.name,
                    context,
                    (api_key, quote(api_key, safe="")),
                    query=kwargs,
                )
                if tool.name == "maps_direction_driving":
                    for item in result["paths"]:
                        origin, destination = (
                            kwargs.get("origin", ""),
                            kwargs.get("destination", ""),
                        )
                        for coordinates in (origin, destination):
                            longitude, latitude = map(float, coordinates.split(","))
                            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                                raise ValueError("invalid_route_coordinates")
                        route = DrivingRouteEvidence(
                            evidence_id="route_" + uuid.uuid4().hex[:16],
                            retrieved_at=datetime.now(UTC),
                            query_origin=origin,
                            query_destination=destination,
                            **item,
                        )
                        context.routes[route.evidence_id] = route
                        item["route_evidence_id"] = route.evidence_id
            except (ValueError, TypeError, AttributeError, RecursionError):
                context.metrics["map_failures"] += 1
                return safe_error("invalid_tool_response")
            context.metrics["compact_result_chars"] += len(
                json.dumps(result, ensure_ascii=False)
            )
            context.cache[key] = (time.monotonic(), copy.deepcopy(result))
            return result

    def wrap(tool):
        async def call(**kwargs):
            return await invoke(tool, kwargs)

        return StructuredTool.from_function(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            coroutine=call,
            infer_schema=False,
        )

    return [wrap(tool) for tool in tools]
