"""ScenePilot 专业子 Agent：风格理解、真实场景检索与路线校验。"""

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from scene_pilot.agents.budgets import budget_middleware

STYLE_PROMPT = """你是人像约拍摄影风格分析专员。
仅从摄影师提供的文字需求中提取服饰颜色、妆造和风格；未提供的材质、发型、妆容细节保持未知。
提出 2-4 组可供真实地图检索的场景关键词，例如"公园 树林"或"复古建筑"；解释颜色、背景、光线的搭配逻辑。
输入没有模特图片或参考图像素时，不得声称已经视觉识别图片；只描述摄影师实际提供的信息。
区分原始事实、风格推断、需要现场验证的条件。不虚构任何真实地点或票价。
汇报不超过 300 中文字：用户原话、2 组检索词、最多 3 条标记为推断的建议；不复述任务。"""

SCENE_PROMPT = """你是摄影场景调研专员。
通过高德 MCP 的 maps_text_search 或 maps_around_search 检索用户指定城市里的真实拍摄候选地点；可用 maps_search_detail 查看地点详情。优先进行 1-2 次有针对性的搜索，不为了凑数连续大量调用工具。
最多推荐 3 个地点，必须原样传回 evidence_id、名称、地址和坐标；不创造缺失信息。已有足够地点时不追加搜索；仅当决策缺少关键基本信息才查详情，同一 POI 不重复查询。工具有运行额度，额度耗尽后停止查询。
高德 POI 名称不能证实某个机位的视觉氛围、季节植被、人流、收费和拍摄许可；以上内容都标为待实地核实。
若用户尚未提供城市，先向主管说明缺少城市，不能擅自查其他城市。查询失败要明确报告。若工具返回 status=error，先利用其他已取得的真实 POI；没有成功结果时明确说明不能核验地点，不得编造。
汇报不超过 600 中文字，完整保留 evidence_id，不抄录冗长工具原文。"""

ROUTE_PROMPT = """你是摄影交通路线专员。
在主管提供出发地、拍摄城市和候选地点后，使用高德 MCP maps_geo 定位必要地点，并通过 maps_direction_driving 查询驾车路线。
用户没提供出发点时不查询路线；地理定位有歧义则要求确认。
只报告地图实际给出的路线里程、时间，不假定拍摄当天路况、票价和包车费用。
如果用户要求公共交通或步行，但目前没有对应工具，明确告知本版本不支持该方式，不能冒充驾车结果。
优先复用已知坐标，最多三条路线；原样保留 route_evidence_id 和目标地点对应关系，汇报不超过 400 中文字。"""


def build_subagents(
    model: BaseChatModel,
    scene_tools: list[BaseTool],
    route_tools: list[BaseTool],
) -> list[dict]:
    specs = [
        {
            "name": "style-agent",
            "description": "从人像约拍的文字描述中提取服饰、妆造、风格、背景和光线的匹配条件；不直接查询地点。",
            "system_prompt": STYLE_PROMPT,
            "tools": [],
            "model": model,
        },
        {
            "name": "scene-agent",
            "description": "通过官方高德 MCP 搜索真实的摄影候选地点，保留 POI 事实并提示现场核验。",
            "system_prompt": SCENE_PROMPT,
            "tools": scene_tools,
            "model": model,
        },
        {
            "name": "route-agent",
            "description": "可选：在已知出发点时通过高德 MCP 查询真实驾车路线与预计耗时。",
            "system_prompt": ROUTE_PROMPT,
            "tools": route_tools,
            "model": model,
        },
    ]
    # CompiledSubAgent is Deep Agents' supported integration point. Specialist
    # graphs do not inherit filesystem, general-purpose or delegation tools.
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "runnable": create_agent(
                model=model,
                tools=spec["tools"],
                system_prompt=spec["system_prompt"],
                middleware=budget_middleware(
                    model_calls=2 if spec["name"] == "style-agent" else 4,
                    tool_calls=6,
                ),
            ),
        }
        for spec in specs
    ]
