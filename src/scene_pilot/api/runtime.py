"""Adapter to the existing bounded planner. Construction performs no network I/O."""

import asyncio
from pathlib import Path

from scene_pilot.agent import create_planning_graph
from scene_pilot.api.models import PlanningResult
from scene_pilot.briefing import DraftResult, extract_brief
from scene_pilot.core.confirmation import BriefDraft, PreparationUsage
from scene_pilot.core.config import get_settings
from scene_pilot.core.execution import ExecutionLimits
from scene_pilot.core.llm import create_qwen_model
from scene_pilot.integrations.amap_mcp import load_amap_tools
from scene_pilot.observability.streaming import StreamObserver


class PlanningRuntime:
    def __init__(self, limits: ExecutionLimits, trace_dir: Path):
        self.limits = limits
        self.trace_dir = trace_dir
        self._model = None
        self._tools = None
        self._loading = asyncio.Lock()

    def _get_model(self):
        if self._model is None:
            self._model = create_qwen_model(get_settings())
        return self._model

    async def extract(self, request):
        try:
            model = self._get_model()
        except Exception:
            return DraftResult(
                BriefDraft(), PreparationUsage(), "model_configuration_required"
            )
        return await extract_brief(model, request, self.limits)

    async def _planner(self):
        async with self._loading:
            if self._tools is None:
                key = get_settings().amap_web_key
                if not key:
                    raise ValueError("map_configuration_required")
                async with asyncio.timeout(25):
                    self._tools = await load_amap_tools(key)
        scene, routes, weather = self._tools
        graph = create_planning_graph(self._get_model(), scene, routes, weather)
        geocode = next((tool for tool in routes if tool.name == "maps_geo"), None)
        return graph, geocode

    async def plan(self, request, confirmed, select_origin):
        observer = StreamObserver(
            self.trace_dir, emit=lambda _: None, limits=self.limits
        )
        error_code = None
        try:
            graph, geocode = await self._planner()
            summary = await observer.arun(
                graph,
                request,
                confirmed_brief=confirmed,
                geocode_tool=geocode,
                select_origin=select_origin,
            )
            status = summary.status
        except asyncio.CancelledError:
            # Observer already cancelled its upstream coroutine and assembled partial evidence.
            status = "cancelled"
        except Exception:
            # Never serialize provider exceptions, raw graph output, or settings.
            status, error_code = "failed", "planning_failed"
        metrics = observer.metrics or {
            "model_attempts": confirmed.preparation_usage.model_attempts,
            "observed_tokens": confirmed.preparation_usage.input_tokens
            + confirmed.preparation_usage.output_tokens,
            "usage_missing": confirmed.preparation_usage.usage_missing,
        }
        return PlanningResult(
            status=status,
            recommendation=observer.recommendation,
            final_text=observer.final_text
            if observer.recommendation is not None
            else "",
            metrics=metrics,
            error_code=error_code,
        )
