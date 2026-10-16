"""Deep Agents 流式执行跟踪：只保留运行元数据，不记录模型内容或工具原始参数。"""

import ast
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langgraph.errors import GraphRecursionError

from scene_pilot.core.execution import (
    ExecutionBudgetExceeded,
    ExecutionLimits,
    execution_scope,
)
from scene_pilot.core.recommendation import (
    CandidateDraft,
    assemble_recommendation,
    render_recommendation,
)

SAFE_STOPS = (
    ExecutionBudgetExceeded,
    ModelCallLimitExceededError,
    ToolCallLimitExceededError,
    GraphRecursionError,
    TimeoutError,
)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    final_text: str
    trace_path: Path
    model_calls: int
    tool_calls: int
    tool_failures: int
    input_tokens: int
    output_tokens: int
    elapsed_seconds: float
    status: str = "completed"
    metrics: dict = field(default_factory=dict)
    recommendation: dict | None = None


def _tool_status(message: Any) -> str:
    if getattr(message, "status", None) == "error":
        return "error"
    content = getattr(message, "content", "")
    if isinstance(content, dict):
        data = content
    elif isinstance(content, str) and len(content) <= 20000:
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            try:
                data = ast.literal_eval(content)
            except (SyntaxError, ValueError, TypeError, MemoryError):
                data = None
    else:
        data = None
    if isinstance(data, dict) and data.get("status") in {"success", "error", "warning"}:
        return data["status"]
    return "executed"


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


class StreamObserver:
    """消费 graph.stream(version='v2') 的 updates/messages，生成安全、可重复测试的进度记录。"""

    def __init__(
        self,
        trace_dir: Path = Path("logs/agent_runs"),
        emit=print,
        *,
        limits: ExecutionLimits | None = None,
        require_evidence: bool = True,
    ):
        self.trace_dir = Path(trace_dir)
        self.emit = emit
        self.tool_started: dict[str, tuple[str, float]] = {}
        self.tool_finished: set[str] = set()
        self.seen_messages: set[str] = set()
        self.active_llm_scopes: set[tuple[str, ...]] = set()
        self.model_calls = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.final_text = ""
        self.run_id = uuid.uuid4().hex[:12]
        self.trace_path = self.trace_dir / f"{self.run_id}.jsonl"
        self.started = time.perf_counter()
        self._trace_file = None
        self.limits = limits or ExecutionLimits()
        self.require_evidence = require_evidence
        self.status = "completed"
        self.metrics = {}
        self.recommendation = None

    def _finish(self, context):
        self.metrics = dict(context.metrics)
        if self.require_evidence:
            plan = context.recommendation
            if plan is None:
                selected, seen = [], set()
                for evidence in context.evidence.values():
                    identity = evidence.poi_id or (evidence.name, evidence.location)
                    if identity not in seen:
                        selected.append(
                            CandidateDraft(evidence_id=evidence.evidence_id)
                        )
                        seen.add(identity)
                    if len(selected) == 3:
                        break
                plan = assemble_recommendation(
                    context.request,
                    [],
                    selected,
                    context.evidence,
                    conditions=context.conditions,
                )
                if plan.candidates:
                    plan = plan.model_copy(update={"status": "partial"})
                if self.status == "completed":
                    self.status = "incomplete"
            elif self.status != "completed":
                plan = plan.model_copy(update={"status": "partial"})
            self.recommendation = plan.model_dump(mode="json")
            self.final_text = render_recommendation(plan)
            if self.status != "completed":
                self.final_text = (
                    "本次规划未完整完成；以下仅展示已取得的地图证据。\n"
                    + self.final_text
                )
        self._record(
            "run_completed" if self.status == "completed" else "run_stopped",
            status=self.status,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            tool_failures=self.tool_failures,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            elapsed_seconds=round(time.perf_counter() - self.started, 2),
            **self.metrics,
        )

    def _summary(self):
        return RunSummary(
            run_id=self.run_id,
            final_text=self.final_text,
            trace_path=self.trace_path,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            tool_failures=self.tool_failures,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            elapsed_seconds=round(time.perf_counter() - self.started, 2),
            status=self.status,
            metrics=self.metrics,
            recommendation=self.recommendation,
        )

    def _record(self, event: str, **fields) -> None:
        data = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        self._trace_file.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._trace_file.flush()

    @staticmethod
    def _scope(namespace: tuple) -> str:
        return "subagent" if namespace else "supervisor"

    def _handle_message(self, message: Any, namespace: tuple) -> None:
        call_items = getattr(message, "tool_calls", None) or []
        for call in call_items:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or f"missing-{len(self.tool_started)}")
            if call_id in self.tool_started:
                continue
            name = str(call.get("name", "unknown"))[:80]
            self.tool_started[call_id] = (name, time.perf_counter())
            self.tool_calls += 1
            if name == "task":
                subagent = str(
                    (call.get("args") or {}).get("subagent_type", "unknown")
                )[:60]
                self.emit(f"  → 委派子 Agent：{subagent}")
                self._record(
                    "subagent_delegated",
                    scope=self._scope(namespace),
                    subagent=subagent,
                )
            else:
                self.emit(f"  → [{self._scope(namespace)}] 调用工具：{name}")
                self._record("tool_requested", scope=self._scope(namespace), tool=name)

        message_type = getattr(message, "type", "")
        if message_type == "tool":
            call_id = str(getattr(message, "tool_call_id", ""))
            if call_id in self.tool_finished:
                return
            self.tool_finished.add(call_id)
            tool_name, start = self.tool_started.get(
                call_id,
                (str(getattr(message, "name", "tool"))[:80], time.perf_counter()),
            )
            duration_ms = round((time.perf_counter() - start) * 1000)
            status = _tool_status(message)
            if status == "error":
                self.tool_failures += 1
            self.emit(
                f"  {'✗' if status == 'error' else '✓'} 工具完成：{tool_name} | {status} | {duration_ms} ms"
            )
            self._record(
                "tool_completed",
                scope=self._scope(namespace),
                tool=tool_name,
                status=status,
                elapsed_ms=duration_ms,
            )

        if message_type != "ai":
            return
        msg_id = str(getattr(message, "id", None) or id(message))
        if msg_id not in self.seen_messages:
            self.seen_messages.add(msg_id)
            self.model_calls += 1
            usage = getattr(message, "usage_metadata", None) or {}
            if isinstance(usage, dict):
                self.input_tokens += int(usage.get("input_tokens") or 0)
                self.output_tokens += int(usage.get("output_tokens") or 0)
        if not namespace and not call_items:
            text = _content_as_text(getattr(message, "content", ""))
            if text:
                self.final_text = text

    def _consume(self, part: dict) -> None:
        if not isinstance(part, dict):
            return
        mode, namespace, payload = (
            part.get("type"),
            tuple(part.get("ns") or ()),
            part.get("data"),
        )
        if mode == "messages":
            if (
                namespace not in self.active_llm_scopes
                and isinstance(payload, (tuple, list))
                and len(payload) == 2
            ):
                chunk, _metadata = payload
                if (
                    getattr(chunk, "type", "") in ("AIMessageChunk", "ai")
                    or chunk.__class__.__name__ == "AIMessageChunk"
                ):
                    self.active_llm_scopes.add(namespace)
                    self.emit(f"  … [{self._scope(namespace)}] 模型正在生成响应")
            return
        if mode != "updates" or not isinstance(payload, dict):
            return
        for state_update in payload.values():
            if not isinstance(state_update, dict):
                continue
            messages = state_update.get("messages") or []
            if not isinstance(messages, (list, tuple)):
                continue
            for message in messages:
                self._handle_message(message, namespace)

    def run(
        self, agent, request: str, recursion_limit: int | None = None
    ) -> RunSummary:
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.emit(f"\n[ScenePilot] 正在规划，运行 ID：{self.run_id}")
        self.started = time.perf_counter()
        with (
            execution_scope(request, self.limits) as context,
            self.trace_path.open("w", encoding="utf-8") as trace_file,
        ):
            self._trace_file = trace_file
            self._record("run_started")
            try:
                if len(request) > self.limits.max_input_chars:
                    raise ExecutionBudgetExceeded("request_too_long")
                for part in agent.stream(
                    {"messages": [{"role": "user", "content": request}]},
                    config={
                        "recursion_limit": recursion_limit
                        or self.limits.recursion_limit
                    },
                    stream_mode=["updates", "messages"],
                    subgraphs=True,
                    version="v2",
                ):
                    self._consume(part)
                    if time.perf_counter() - self.started > self.limits.timeout_seconds:
                        raise ExecutionBudgetExceeded("elapsed_time")
            except SAFE_STOPS as exc:
                self.status = "budget_exhausted"
                self._record("budget_stop", reason=type(exc).__name__)
            except Exception as exc:
                self.status = "failed"
                self._record("run_failed", error_type=type(exc).__name__)
                raise
            finally:
                self._finish(context)
                self._trace_file = None
        return self._summary()

    async def arun(
        self, agent, request: str, recursion_limit: int | None = None
    ) -> RunSummary:
        """消费异步 LangGraph v2 流，供 MCP 异步工具使用。"""
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.emit(f"\n[ScenePilot] 正在规划，运行 ID：{self.run_id}")
        self.started = time.perf_counter()
        with (
            execution_scope(request, self.limits) as context,
            self.trace_path.open("w", encoding="utf-8") as trace_file,
        ):
            self._trace_file = trace_file
            self._record("run_started")
            try:
                if len(request) > self.limits.max_input_chars:
                    raise ExecutionBudgetExceeded("request_too_long")
                async with asyncio.timeout(self.limits.timeout_seconds):
                    async for part in agent.astream(
                        {"messages": [{"role": "user", "content": request}]},
                        config={
                            "recursion_limit": recursion_limit
                            or self.limits.recursion_limit
                        },
                        stream_mode=["updates", "messages"],
                        subgraphs=True,
                        version="v2",
                    ):
                        self._consume(part)
            except SAFE_STOPS as exc:
                self.status = "budget_exhausted"
                self._record("budget_stop", reason=type(exc).__name__)
            except asyncio.CancelledError:
                self.status = "cancelled"
                self._record("run_cancelled")
                raise
            except Exception as exc:
                self.status = "failed"
                self._record("run_failed", error_type=type(exc).__name__)
                raise
            finally:
                self._finish(context)
                self._trace_file = None
        return self._summary()
