"""Official middleware hooks for domain tool isolation and shared run budgets."""

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.messages import SystemMessage, ToolMessage

from scene_pilot.core.execution import current_run


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

    def __init__(self, allowed: set[str], prompt: str):
        self.allowed = allowed
        self.prompt = prompt

    def _request(self, request):
        tools = [
            item
            for item in request.tools
            if (item.get("name") if isinstance(item, dict) else item.name)
            in self.allowed
        ]
        return request.override(tools=tools, system_message=SystemMessage(self.prompt))

    def wrap_model_call(self, request, handler):
        return handler(self._request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request(request))

    def _deny(self, request):
        if request.tool_call["name"] not in self.allowed:
            return ToolMessage(
                content='{"status":"error","error_code":"tool_not_allowed"}',
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


def budget_middleware(model_calls: int, tool_calls: int):
    return [
        ModelCallLimitMiddleware(run_limit=model_calls, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=tool_calls, exit_behavior="error"),
        RunBudgetMiddleware(),
    ]
