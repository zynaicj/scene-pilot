"""GroupPilot Deep Agents 主规划智能体。"""
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from group_pilot.agents.subagents import build_subagents
from group_pilot.core.config import get_settings
from group_pilot.core.llm import create_qwen_model
from group_pilot.tools.budget_tools import calculate_group_budget


SUPERVISOR_PROMPT = """你是 GroupPilot 的主规划 Agent，面向高校课题组与多人团队出行。
1. 识别出行日期、城市、准确出发地、人数、预算和偏好；缺少核心信息时先询问，不得自行指定校区或日期。
2. 对复杂任务使用 write_todos。先委派 destination-agent 检索真实候选目的地，再将候选地坐标、原始出发地传给 route-agent。子 Agent 不共享完整主对话，所以委派内容必须包含所有必要参数。
3. 如果用户提供人数与人均预算，可调用 calculate_group_budget；该结果仅代表预算上限，不是实际报价。
4. 汇总来源可信的地点名称、坐标、距离和路线时间；报告无数据、工具失败及需人工确认的部分。
5. 当前仅提供地点与驾车路线调研，不具备真实票价、团队交通报价、天气核查、预订或完整硬约束验证；不得宣称方案预算合规或可直接执行。
6. 不要操作本地文件、运行 shell 或使用 general-purpose 子 Agent。使用简洁中文回复。
"""


def build_group_pilot_agent():
    settings = get_settings()
    model = create_qwen_model(settings)
    return create_deep_agent(model=model, tools=[calculate_group_budget], subagents=build_subagents(model), middleware=[TodoListMiddleware()], system_prompt=SUPERVISOR_PROMPT)
