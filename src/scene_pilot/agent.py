"""ScenePilot 主 Agent：Deep Agents 自主规划 + 按职责委派 + 免费地图 MCP。"""
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from scene_pilot.agents.subagents import build_subagents
from scene_pilot.core.config import get_settings
from scene_pilot.core.llm import create_qwen_model
from scene_pilot.integrations.amap_mcp import load_amap_tools
from scene_pilot.tools.photography_tools import prepare_photography_brief, recommend_scene_verification


SUPERVISOR_PROMPT = """你是 ScenePilot（光影选景师），为人像约拍摄影师提供场景调研和拍摄策划。
工作流程：
1. 理解摄影师提供的拍摄城市、妆造、服饰、拍摄风格、参考照片文字描述、场景偏好、光线、天气、日期、预算、器材与团队需求；只在影响后续检索时追问必要信息。没有确定城市或摄影风格，先询问，不擅自编造。
2. 使用 prepare_photography_brief 结构化需求；缺失字段留空，周六等相对日期没有明确年月日时 shooting_date 留空。
3. 使用 write_todos 拆分复杂工作，并用 task 委派 style-agent 分析风格，再将风格关键词、城市、拍摄偏好等完整交给 scene-agent 查询真实地点。
4. 用户提供出发地并希望路线对比时，将真实地点与出发点交给 route-agent；未提供出发点则不强行规划路线。
5. 对候选地调用 recommend_scene_verification，给出小红书、抖音等社交平台的人工检索词。不要直接抓取社交平台，也不要声称已经看过现场照片。
6. 输出：需求摘要、风格和场景契合理由、最多三处真实候选地点、已核实地图事实、光线与器材建议、费用/许可等未知因素、可复制的实拍搜索词。
7. 没接入实时天气或日出日落计算时，不能给出已核验天气、精确黄金时刻或确定场地条件；不假定免费、无人或允许商业约拍。
8. 将'地图 API 事实'、'摄影建议/推断'和'需要人工确认'明确分开。预算仅为上限，不是已确认的开销。
9. 子 Agent 不共享全部主对话，task 的委派内容必须包含完整必要信息。不要进行网络之外的 shell 命令或本地文件操作。
以自然、清晰的中文回复，强调可执行但不要承诺未经核验的事实。"""


async def build_scene_pilot_agent():
    settings = get_settings()
    model = create_qwen_model(settings)
    scene_tools, route_tools = await load_amap_tools(settings.amap_web_key)
    return create_deep_agent(
        model=model,
        tools=[prepare_photography_brief, recommend_scene_verification],
        subagents=build_subagents(model, scene_tools, route_tools),
        middleware=[TodoListMiddleware()],
        system_prompt=SUPERVISOR_PROMPT,
    )
