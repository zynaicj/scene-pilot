"""Per-request budgets and evidence; never persist private request content."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scene_pilot.core.confirmation import ConfirmedBrief


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    map_attempts: int = Field(default=8, ge=1, le=50)
    search_attempts: int = Field(default=2, ge=1, le=10)
    detail_attempts: int = Field(default=3, ge=0, le=10)
    weather_attempts: int = Field(default=2, ge=0, le=5)
    model_calls: int = Field(default=12, ge=1, le=50)
    observed_tokens: int = Field(default=40000, ge=1)
    recursion_limit: int = Field(default=40, ge=2, le=200)
    timeout_seconds: float = Field(default=180, gt=0)
    cache_ttl_seconds: float = Field(default=300, gt=0)
    max_pois: int = Field(default=6, ge=1, le=20)
    max_input_chars: int = Field(default=12000, ge=1, le=100000)


class ExecutionBudgetExceeded(RuntimeError):
    """A safe fixed code, without request data or provider exception text."""


@dataclass
class RunContext:
    request: str = field(repr=False)
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    metrics: dict[str, int | float] = field(
        default_factory=lambda: {
            "map_requests": 0,
            "map_attempts": 0,
            "search_attempts": 0,
            "detail_attempts": 0,
            "weather_attempts": 0,
            "solar_calculations": 0,
            "cache_hits": 0,
            "map_retries": 0,
            "map_failures": 0,
            "budget_rejections": 0,
            "map_elapsed_ms": 0,
            "raw_result_chars": 0,
            "compact_result_chars": 0,
            "model_attempts": 0,
            "observed_tokens": 0,
            "usage_missing": 0,
        }
    )
    cache: dict = field(default_factory=dict, repr=False)
    evidence: dict = field(default_factory=dict, repr=False)
    routes: dict = field(default_factory=dict, repr=False)
    weather: dict = field(default_factory=dict, repr=False)
    conditions: dict = field(default_factory=dict, repr=False)
    geocodes: dict = field(default_factory=dict, repr=False)
    departure: Any = field(default=None, repr=False)
    confirmed_brief: ConfirmedBrief | None = field(default=None, repr=False)
    brief: Any = field(default=None, repr=False)
    recommendation: Any = field(default=None, repr=False)
    lock: Lock = field(default_factory=Lock, repr=False)

    def reserve_model(self) -> None:
        with self.lock:
            if self.metrics["model_attempts"] >= self.limits.model_calls:
                raise ExecutionBudgetExceeded("model_calls")
            if self.metrics["observed_tokens"] >= self.limits.observed_tokens:
                raise ExecutionBudgetExceeded("observed_tokens")
            self.metrics["model_attempts"] += 1


CURRENT_RUN: ContextVar[RunContext | None] = ContextVar("scene_pilot_run", default=None)


def current_run() -> RunContext:
    context = CURRENT_RUN.get()
    if context is None:
        raise ExecutionBudgetExceeded("run_context_required")
    return context


@contextmanager
def execution_scope(
    request: str,
    limits: ExecutionLimits | None = None,
    *,
    confirmed_brief: ConfirmedBrief | None = None,
):
    context = RunContext(request=request, limits=limits or ExecutionLimits())
    if confirmed_brief is not None:
        if (
            not isinstance(confirmed_brief, ConfirmedBrief)
            or confirmed_brief.request != request
        ):
            raise ValueError("confirmation_request_mismatch")
        context.confirmed_brief = confirmed_brief
        context.brief = confirmed_brief.brief
        usage = confirmed_brief.preparation_usage
        context.metrics.update(
            model_attempts=usage.model_attempts,
            observed_tokens=usage.input_tokens + usage.output_tokens,
            usage_missing=usage.usage_missing,
        )
    token = CURRENT_RUN.set(context)
    try:
        yield context
    finally:
        CURRENT_RUN.reset(token)
