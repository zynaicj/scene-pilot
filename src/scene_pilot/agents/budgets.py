"""Official middleware hooks for domain tool isolation and shared run budgets."""

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    hook_config,
)
from langchain_core.messages import SystemMessage, ToolMessage

from scene_pilot.core.execution import current_run


class DisabledSummarizationMiddleware(AgentMiddleware):
    """Replace the already disabled summarizer without a no-op graph node."""

    @property
    def name(self):
        # Deep Agents replaces its default middleware by public middleware name.
        return "SummarizationMiddleware"


class FinalizingModelCallLimitMiddleware(ModelCallLimitMiddleware):
    """Stop after a validated submission, using the existing budget-check node."""

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        if current_run().recommendation is not None:
            return {"jump_to": "end"}
        # Failed validation leaves recommendation unset: allow correction while
        # preserving the original per-agent model limit and error behavior.
        return super().before_model(state, runtime)


class RunBudgetMiddleware(AgentMiddleware):
    def _observe(self, response):
        context = current_run()
        for message in response.result:
            usage = getattr(message, "usage_metadata", None)
            if getattr(message, "type", None) != "ai":
                continue
            with context.lock:
                if usage is None:
                    context.metrics["usage_missing"] += 1
                else:
                    context.metrics["observed_tokens"] += usage.get(
                        "input_tokens", 0
                    ) + usage.get("output_tokens", 0)
        return response

    def wrap_model_call(self, request, handler):
        current_run().reserve_model()
        return self._observe(handler(request))

    async def awrap_model_call(self, request, handler):
        current_run().reserve_model()
        return self._observe(await handler(request))


class DomainToolsMiddleware(AgentMiddleware):
    """Remove generic filesystem prose/tools while retaining Deep Agents task/todos."""

    def __init__(self, allowed: set[str], prompt: str, phase: str = "supervisor"):
        self.allowed = allowed
        self.prompt = prompt
        self.phase = phase

    def _available(self, messages):
        allowed = set(self.allowed)
        if self.phase == "supervisor" and any(
            isinstance(message, ToolMessage) and message.name == "write_todos"
            for message in messages
        ):
            allowed.discard("write_todos")
        if self.phase == "scene":
            context = current_run()
            identities = {
                entry.poi_id or (entry.name, entry.location)
                for entry in context.evidence.values()
            }
            if (
                len(identities) >= 3
                or context.metrics["search_attempts"] >= context.limits.search_attempts
                or context.metrics["map_attempts"] >= context.limits.map_attempts
            ):
                allowed -= {"maps_text_search", "maps_around_search"}
        return allowed

    def _request(self, request):
        allowed = self._available(request.messages)
        tools = [
            item
            for item in request.tools
            if (item.get("name") if isinstance(item, dict) else item.name) in allowed
        ]
        prompt = self.prompt
        if allowed != self.allowed:
            prompt += (
                "\n本轮已移除不再需要的工具。不要重复更新计划或换搜索工具绕过停止条件。"
                "已有证据时推进后续步骤；搜索额度耗尽时汇报已有候选，不为了凑足三个继续搜索。"
            )
        return request.override(tools=tools, system_message=SystemMessage(prompt))

    def wrap_model_call(self, request, handler):
        return handler(self._request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request(request))

    def _deny(self, request):
        if request.tool_call["name"] not in self._available(request.state["messages"]):
            return ToolMessage(
                content=(
                    '{"status":"error","error_code":"tool_not_allowed",'
                    '"message":"该工具当前不可用；利用已有证据推进后续步骤，不要重复调用。"}'
                ),
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return None

    def wrap_tool_call(self, request, handler):
        denied = self._deny(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denied = self._deny(request)
        return denied if denied is not None else await handler(request)


def budget_middleware(model_calls: int, tool_calls: int, *, finish_on_submission=False):
    model_limiter = (
        FinalizingModelCallLimitMiddleware
        if finish_on_submission
        else ModelCallLimitMiddleware
    )
    return [
        model_limiter(run_limit=model_calls, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=tool_calls, exit_behavior="error"),
        RunBudgetMiddleware(),
    ]
