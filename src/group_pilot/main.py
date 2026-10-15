"""GroupPilot 正式命令行入口。"""
from group_pilot.agent import build_group_pilot_agent
from group_pilot.core.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.qwen_api_key or not settings.amap_web_key:
        print("请在项目根目录 .env 中配置 DASHSCOPE_API_KEY 和 AMAP_WEB_KEY")
        return
    print("GroupPilot V0.2 已启动。输入完整团建需求；输入 exit 退出。")
    agent = build_group_pilot_agent()
    while True:
        try:
            request = input("\n团建需求 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            return
        if request.lower() in {"exit", "quit", "退出"}:
            break
        if not request:
            continue
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": request}]}, config={"recursion_limit": 60})
            print("\n===== 出行调研结果 =====")
            print(result["messages"][-1].content)
            print("\n===== 子 Agent 委派 =====")
            for message in result["messages"]:
                for call in getattr(message, "tool_calls", []):
                    if call.get("name") == "task":
                        print(f"task → {call.get('args', {}).get('subagent_type', 'unknown')}")
            print("注意：当前每次输入是独立任务，尚不支持跨轮上下文恢复。")
        except Exception as exc:
            print(f"规划失败（{type(exc).__name__}），请检查配置或模型服务；不要据此认定路线已规划成功。")


if __name__ == "__main__":
    main()
