"""官方高德 Streamable HTTP MCP 接入，按职责最小化开放工具。

密钥仅在 MCP 客户端 URL 中供连接使用；不要打印 URL、原始异常或客户端配置。
"""
from urllib.parse import quote
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient


SCENE_TOOL_NAMES = frozenset({"maps_text_search", "maps_around_search", "maps_search_detail"})
ROUTE_TOOL_NAMES = frozenset({"maps_geo", "maps_direction_driving"})
ALLOWED_TOOL_NAMES = SCENE_TOOL_NAMES | ROUTE_TOOL_NAMES


class AmapMCPError(RuntimeError):
    """MCP 服务连接或必要工具发现失败。"""


def build_amap_mcp_client(api_key: str) -> MultiServerMCPClient:
    if not api_key.strip():
        raise AmapMCPError("缺少 AMAP_WEB_KEY，请在本地 .env 中配置高德 Web 服务 Key")
    # 官方文档的 MCP endpoint；不要把这里包含密钥的 URL 输出到日志。
    url = f"https://mcp.amap.com/mcp?key={quote(api_key.strip(), safe='')}"
    return MultiServerMCPClient({"amap": {"transport": "http", "url": url}}, handle_tool_errors=True)


def select_amap_tools(tools: list[BaseTool]) -> tuple[list[BaseTool], list[BaseTool]]:
    """只允许选景和路线相关工具，并在关键工具缺失时中止启动。"""
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
        all_tools = await client.get_tools(server_name="amap")
    except Exception:
        # 底层异常可能包含带 API Key 的请求 URL，不能原样传播到终端或日志。
        raise AmapMCPError("连接高德 MCP 失败；请检查网络、Key、个人额度及 MCP 服务状态") from None
    return select_amap_tools(all_tools)
