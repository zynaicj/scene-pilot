
import os

from dotenv import load_dotenv
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain.agents.middleware import TodoListMiddleware


load_dotenv()


# ========== 1. 定义工具 ==========

@tool
def calculate_group_budget(member_count: int,budget_per_person: float,) -> dict:
    """计算团队总预算。

    Args:
        member_count: 参加活动的人数
        budget_per_person: 每个人的预算，单位为元
    """
    if member_count <= 0 or budget_per_person < 0:
        return {"status": "error", "message": "预算参数不合法"}

    total = member_count * budget_per_person

    return {
        "status": "success",
        "member_count": member_count,
        "budget_per_person": budget_per_person,
        "total_budget": round(total, 2),
        "currency": "CNY",
    }


# ========== 2. 初始化 Qwen ==========

model = ChatOpenAI(
    model=os.environ["QWEN_MODEL"],
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url=os.environ["QWEN_BASE_URL"],
    temperature=0,
)


# ========== 3. 创建 Deep Agent ==========

agent = create_deep_agent(
    model=model,
    tools=[calculate_group_budget],
    middleware=[TodoListMiddleware()],
    system_prompt="""
你是 GroupPilot 团体出行规划平台的主 Agent。

你的职责：
1. 理解用户提出的多人出行需求。
2. 对复杂任务使用 write_todos 制定任务清单。
3. 使用工具完成确定性计算。
4. 明确区分已知信息和待查询信息。
5. 不得虚构地图路线、实时价格和天气数据。

当前为开发测试阶段：
只需要计算预算并拆解出行规划任务，
不需要调用真实地图或生成最终旅游方案。
""",
)


# ========== 4. 测试 Agent ==========

def main():
    user_request = """
    我们是杭州的一所高校课题组，
    计划组织一次周末团建。

    一共有12个人，每人预算250元。

    请完成以下任务：
    1. 调用预算工具计算总预算。
    2. 使用 write_todos 拆解后续规划任务。
    3. 告诉我规划需要哪些信息。
    """

    result = agent.invoke(
        {
            "messages": [
                {"role": "user", "content": user_request}
            ]
        },
        config={"recursion_limit": 30},
    )

    print("\n========== Agent 最终回复 ==========")
    print(result["messages"][-1].content)

    print("\n========== 工具调用记录 ==========")

    for message in result["messages"]:
        tool_calls = getattr(message, "tool_calls", [])

        for call in tool_calls:
            print(f"工具名称: {call['name']}")
            print(f"调用参数: {call['args']}")
            print("-" * 40)


if __name__ == "__main__":
    main()
