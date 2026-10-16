"""ScenePilot 主 Agent：Deep Agents 自主规划 + 按职责委派 + 免费地图 MCP。"""

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import TodoListMiddleware

from scene_pilot.agents.budgets import (
    DisabledSummarizationMiddleware,
    DomainToolsMiddleware,
    budget_middleware,
)
from scene_pilot.agents.subagents import build_subagents
from scene_pilot.core.config import get_settings
from scene_pilot.core.llm import create_qwen_model
from scene_pilot.integrations.amap_mcp import load_amap_tools
from scene_pilot.tools.photography_tools import (
    prepare_photography_brief,
    recommend_scene_verification,
)
from scene_pilot.tools.recommendation_tools import finalize_recommendation
from scene_pilot.tools.environment_tools import build_conditions_tool

SUPERVISOR_PROMPT = """你是 ScenePilot（光影选景师），为人像约拍摄影师提供场景调研和拍摄策划。
工作流程：
1. 理解摄影师提供的拍摄城市、妆造、服饰、拍摄风格、参考照片文字描述、场景偏好、光线、天气、日期、预算、器材与团队需求；只在影响后续检索时追问必要信息。没有确定城市或摄影风格，先询问，不擅自编造。
2. 使用 prepare_photography_brief 结构化并保存本次需求；缺失字段留空，只有用户原文中存在 YYYY-MM-DD 时填写 shooting_date，周六等相对日期留空。
3. write_todos 仅在开始时调用一次，与 prepare_photography_brief 同轮调用，不单独占用一轮；之后不逐项更新完成状态。用 task 委派 style-agent 分析风格，再将关键词、城市和偏好交给 scene-agent。已有 1-3 个可用候选时推进后续工作，不重新委派搜索或为了凑满三个继续查询。
4. 用户提供出发地并希望路线对比时，仅将有真实坐标的候选交给 route-agent；路线委派可与 inspect_shooting_conditions 同轮发起，等待结果再提交。出发地或候选坐标缺失时说明路线未知并跳过，不能因此阻塞天气查询和最终提交。
5. 取得候选地点后，将最多三个 evidence_id 一次传给 inspect_shooting_conditions；它使用已保存日期和地图区域查询天气、本地计算太阳时段。不得自行填天气、经纬度或日期；失败和不覆盖日期都按未知处理。随后必须调用 finalize_recommendation：user_statements 只引用用户原话，candidates 只传本次高德工具返回的 evidence_id 和 AI 推断建议。如有路线，附对应 route_evidence_id。后端自动附加天气光照报告与人工检索词。
6. 输出：需求摘要、风格和场景契合理由、最多三处真实候选地点、已核实地图事实、光线与器材建议、费用/许可等未知因素、可复制的实拍搜索词。
7. 区域逐日天气预报不等于机位实况；本地天文时段不保证现场有光。没有工具结果时保持未知，不假定免费、无人或允许商业约拍。
8. 将'地图 API 事实'、'摄影建议/推断'和'需要人工确认'明确分开。预算仅为上限，不是已确认的开销。
9. 子 Agent 不共享全部主对话，task 的委派内容必须包含完整必要信息。不要进行网络之外的 shell 命令或本地文件操作。
10. 仅委派 style-agent、scene-agent 和按需 route-agent，每类一次即可；不携带对话历史或完整工具原文，只传必要条件。收到证据后尽快提交方案，不额外扩写。
以自然、清晰的中文回复，强调可执行但不要承诺未经核验的事实。"""


async def build_scene_pilot_agent():
    settings = get_settings()
    model = create_qwen_model(settings)
    scene_tools, route_tools, weather_tools = await load_amap_tools(
        settings.amap_web_key
    )
    return create_planning_graph(model, scene_tools, route_tools, weather_tools)


def create_planning_graph(model, scene_tools, route_tools, weather_tools=None):
    subagents = build_subagents(model, scene_tools, route_tools)
    local_tools = [
        prepare_photography_brief,
        recommend_scene_verification,
        finalize_recommendation,
        build_conditions_tool(weather_tools or []),
    ]
    return create_deep_agent(
        model=model,
        tools=local_tools,
        subagents=subagents,
        middleware=[
            TodoListMiddleware(),
            # No summary model call AND no dormant before_model graph step.
            DisabledSummarizationMiddleware(),
            SubAgentMiddleware(
                backend=StateBackend(),
                subagents=subagents,
                system_prompt="按职责委派；提供完整必要条件；保留子 Agent 返回的证据编号。",
                task_description="委派 style-agent、scene-agent 或 route-agent。传入必要需求，汇总其简短报告。",
            ),
            *budget_middleware(
                model_calls=8,
                tool_calls=12,
                finish_on_submission=True,
            ),
            DomainToolsMiddleware(
                allowed={tool.name for tool in local_tools} | {"write_todos", "task"},
                prompt=SUPERVISOR_PROMPT,
            ),
        ],
        system_prompt=SUPERVISOR_PROMPT,
    )
