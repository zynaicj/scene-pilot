"""高德 Streamable HTTP MCP 工具接入与安全的传输故障恢复。

高德 Web Key 只参与构造客户端 URL，不写入 Trace、模型消息或异常输出。
"""

import asyncio
import uuid
from urllib.parse import quote

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from scene_pilot.core.execution import CURRENT_RUN
from scene_pilot.integrations.amap_policy import govern_amap_tools
from scene_pilot.integrations.amap_results import safe_error

SCENE_TOOL_NAMES = frozenset(
    {"maps_text_search", "maps_around_search", "maps_search_detail"}
)
ROUTE_TOOL_NAMES = frozenset({"maps_geo", "maps_direction_driving"})
ALLOWED_TOOL_NAMES = SCENE_TOOL_NAMES | ROUTE_TOOL_NAMES

# 高德是只读地图服务：短暂断线后重试至多一次，且不会重试服务商明确的业务错误。
MAX_TRANSPORT_RETRIES = 1
MCP_CALL_TIMEOUT_SECONDS = 25
RETRY_DELAY_SECONDS = 0.5
TRANSIENT_EXCEPTION_NAMES = frozenset(
    {
        "BrokenResourceError",
        "ClosedResourceError",
        "EndOfStream",
        "RemoteProtocolError",
        "ReadError",
        "ReadTimeout",
        "ConnectError",
        "ConnectTimeout",
        "WriteError",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "ConnectionResetError",
        "ConnectionAbortedError",
    }
)


class AmapMCPError(RuntimeError):
    """MCP 服务连接或必要工具发现失败。"""


def _transient_transport_error(exc: Exception) -> bool:
    """识别 HTTPX/AnyIO 通信错误；不把 400/403 等业务错误当成可重试异常。"""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if type(exc).__name__ in TRANSIENT_EXCEPTION_NAMES:
        return True
    if isinstance(exc, ExceptionGroup):
        children = [child for child in exc.exceptions if isinstance(child, Exception)]
        return (
            bool(children)
            and len(children) == len(exc.exceptions)
            and all(_transient_transport_error(child) for child in children)
        )
    return False


def protect_amap_tools(
    tools: list[BaseTool],
    *,
    max_concurrency: int = 1,
    timeout_seconds: float = MCP_CALL_TIMEOUT_SECONDS,
    max_retries: int = MAX_TRANSPORT_RETRIES,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
) -> list[BaseTool]:
    """限制并发并包裹 MCP 调用；错误结果不包含异常消息或带密钥的请求 URL。

    保护层保留工具名称、描述和输入 schema。每一次重试会重新调用原工具，
    由适配器为此次调用创建新 MCP 会话。
    """
    if (
        max_concurrency < 1
        or timeout_seconds <= 0
        or max_retries < 0
        or retry_delay_seconds < 0
    ):
        raise ValueError("MCP 调用限制参数无效")
    semaphore = asyncio.Semaphore(max_concurrency)

    def wrap(tool: BaseTool) -> BaseTool:
        async def guarded_call(**kwargs):
            async with semaphore:
                for attempt in range(max_retries + 1):
                    context = CURRENT_RUN.get()
                    if context is not None:
                        category = (
                            "search_attempts"
                            if tool.name in {"maps_text_search", "maps_around_search"}
                            else "detail_attempts"
                            if tool.name == "maps_search_detail"
                            else None
                        )
                        if context.metrics[
                            "map_attempts"
                        ] >= context.limits.map_attempts or (
                            category
                            and context.metrics[category]
                            >= getattr(context.limits, category)
                        ):
                            context.metrics["budget_rejections"] += 1
                            return safe_error("map_budget_exceeded")
                        context.metrics["map_attempts"] += 1
                        if category:
                            context.metrics[category] += 1
                        if attempt:
                            context.metrics["map_retries"] += 1
                    try:
                        result = await asyncio.wait_for(
                            tool.ainvoke(
                                {
                                    "type": "tool_call",
                                    "id": uuid.uuid4().hex,
                                    "name": tool.name,
                                    "args": kwargs,
                                }
                            ),
                            timeout=timeout_seconds,
                        )
                        # A ToolCall retains the adapter's error status and structured
                        # artifact. Invoking with plain args silently loses both.
                        if isinstance(result, ToolMessage):
                            if result.status == "error":
                                raise ValueError("MCP provider rejected the request")
                            artifact = result.artifact
                            if (
                                isinstance(artifact, dict)
                                and "structured_content" in artifact
                            ):
                                return artifact["structured_content"]
                            return result.content
                        return result
                    except Exception as exc:  # noqa: BLE001 - provider errors may contain credentials
                        transient = _transient_transport_error(exc)
                        if transient and attempt < max_retries:
                            await asyncio.sleep(retry_delay_seconds * (attempt + 1))
                            continue
                        if context is not None:
                            context.metrics["map_failures"] += 1
                        return {
                            "status": "error",
                            "source": "amap_mcp",
                            "error_code": "transport_unavailable"
                            if transient
                            else "tool_execution_failed",
                            "retryable": transient,
                            "message": (
                                "高德 MCP 连接暂时中断，重试后仍未成功。"
                                if transient
                                else "高德 MCP 工具调用未成功，请检查服务可用性。"
                            ),
                            "guidance": (
                                "此地点查询未完成；可使用其他已成功检索的地点。"
                                "如果没有有效 POI 结果，请明确告知用户并建议稍后重试，不能编造地点。"
                            ),
                        }

        return StructuredTool.from_function(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            coroutine=guarded_call,
            infer_schema=False,
        )

    return [wrap(tool) for tool in tools]


def build_amap_mcp_client(api_key: str) -> MultiServerMCPClient:
    if not api_key.strip():
        raise AmapMCPError("缺少 AMAP_WEB_KEY，请在本地 .env 中配置高德 Web 服务 Key")
    url = f"https://mcp.amap.com/mcp?key={quote(api_key.strip(), safe='')}"
    return MultiServerMCPClient(
        {"amap": {"transport": "http", "url": url}},
        handle_tool_errors=False,
    )


def select_amap_tools(tools: list[BaseTool]) -> tuple[list[BaseTool], list[BaseTool]]:
    """只允许地点和路线相关工具；必需工具缺失时停止启动。"""
    names = {tool.name for tool in tools}
    if "maps_text_search" not in names:
        raise AmapMCPError("高德 MCP 缺少 maps_text_search，无法进行真实地点检索")
    if "maps_direction_driving" not in names:
        raise AmapMCPError("高德 MCP 缺少 maps_direction_driving，无法进行驾车路线查询")
    scene = [tool for tool in tools if tool.name in SCENE_TOOL_NAMES]
    route = [tool for tool in tools if tool.name in ROUTE_TOOL_NAMES]
    return scene, route


async def load_amap_tools(api_key: str) -> tuple[list[BaseTool], list[BaseTool]]:
    client = build_amap_mcp_client(api_key)
    try:
        all_tools = await asyncio.wait_for(
            client.get_tools(server_name="amap"),
            timeout=MCP_CALL_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - never propagate credential-bearing discovery errors
        # HTTP URL 可能包含 Key，所以绝不传播原始异常的文本或堆栈。
        raise AmapMCPError(
            "连接高德 MCP 失败；请检查网络、Key、额度及服务状态"
        ) from None
    scene, route = select_amap_tools(all_tools)
    # 共享一个 semaphore：路线和地点检索也不能彼此并发挤占 MCP 连接。
    protected = govern_amap_tools(protect_amap_tools(scene + route), api_key)
    return (
        [tool for tool in protected if tool.name in SCENE_TOOL_NAMES],
        [tool for tool in protected if tool.name in ROUTE_TOOL_NAMES],
    )
