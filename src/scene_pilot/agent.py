"""ScenePilot 主 Agent：Deep Agents 自主规划 + 按职责委派 + 免费地图 MCP。"""

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import SummarizationMiddleware, TodoListMiddleware

from scene_pilot.agents.budgets import DomainToolsMiddleware, budget_middleware
from scene_pilot.agents.subagents import build_subagents
from scene_pilot.core.config import get_settings
from scene_pilot.core.llm import create_qwen_model
from scene_pilot.integrations.amap_mcp import load_amap_tools
from scene_pilot.tools.photography_tools import (
    prepare_photography_brief,
    recommend_scene_verification,
)
from scene_pilot.tools.recommendation_tools import finalize_recommendation

SUPERVISOR_PROMPT = """你是 ScenePilot（光影选景师），为人像约拍摄影师提供场景调研和拍摄策划。
工作流程：
1. 理解摄影师提供的拍摄城市、妆造、服饰、拍摄风格、参考照片文字描述、场景偏好、光线、天气、日期、预算、器材与团队需求；只在影响后续检索时追问必要信息。没有确定城市或摄影风格，先询问，不擅自编造。
2. 使用 prepare_photography_brief 结构化需求；缺失字段留空，周六等相对日期没有明确年月日时 shooting_date 留空。
3. 使用 write_todos 拆分复杂工作，并用 task 委派 style-agent 分析风格，再将风格关键词、城市、拍摄偏好等完整交给 scene-agent 查询真实地点。
4. 用户提供出发地并希望路线对比时，将真实地点与出发点交给 route-agent；未提供出发点则不强行规划路线。
5. 完成调研后必须调用 finalize_recommendation：user_statements 只引用用户原话，candidates 只传本次高德工具返回的 evidence_id 和 AI 推断建议。如有路线，附对应 route_evidence_id。该工具会自动生成每个候选地的人工检索词，无需逐地调用核验工具。
6. 输出：需求摘要、风格和场景契合理由、最多三处真实候选地点、已核实地图事实、光线与器材建议、费用/许可等未知因素、可复制的实拍搜索词。
7. 没接入实时天气或日出日落计算时，不能给出已核验天气、精确黄金时刻或确定场地条件；不假定免费、无人或允许商业约拍。
8. 将'地图 API 事实'、'摄影建议/推断'和'需要人工确认'明确分开。预算仅为上限，不是已确认的开销。
9. 子 Agent 不共享全部主对话，task 的委派内容必须包含完整必要信息。不要进行网络之外的 shell 命令或本地文件操作。
10. 仅委派 style-agent、scene-agent 和按需 route-agent，每类一次即可；不携带对话历史或完整工具原文，只传必要条件。收到证据后尽快提交方案，不额外扩写。
以自然、清晰的中文回复，强调可执行但不要承诺未经核验的事实。"""


async def build_scene_pilot_agent():
    settings = get_settings()
    model = create_qwen_model(settings)
    scene_tools, route_tools = await load_amap_tools(settings.amap_web_key)
    return create_planning_graph(model, scene_tools, route_tools)


def create_planning_graph(model, scene_tools, route_tools):
    subagents = build_subagents(model, scene_tools, route_tools)
    local_tools = [
        prepare_photography_brief,
        recommend_scene_verification,
        finalize_recommendation,
    ]
    return create_deep_agent(
        model=model,
        tools=local_tools,
        subagents=subagents,
        middleware=[
            TodoListMiddleware(),
            # Short, bounded single-request runs need no extra summarization
            # model call outside the shared execution budget.
            SummarizationMiddleware(model=model, trigger=None),
            SubAgentMiddleware(
                backend=StateBackend(),
                subagents=subagents,
                system_prompt="按职责委派；提供完整必要条件；保留子 Agent 返回的证据编号。",
                task_description="委派 style-agent、scene-agent 或 route-agent。传入必要需求，汇总其简短报告。",
            ),
            *budget_middleware(model_calls=8, tool_calls=12),
            DomainToolsMiddleware(
                allowed={tool.name for tool in local_tools} | {"write_todos", "task"},
                prompt=SUPERVISOR_PROMPT,
            ),
        ],
        system_prompt=SUPERVISOR_PROMPT,
    )
