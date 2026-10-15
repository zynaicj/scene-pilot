"""GroupPilot 正式命令行入口：实时进度 + 安全结构化 Trace。"""
from group_pilot.agent import build_group_pilot_agent
from group_pilot.core.config import get_settings
from group_pilot.observability.streaming import StreamObserver


def main() -> None:
    settings = get_settings()
    if not settings.qwen_api_key or not settings.amap_web_key:
        print("请在项目根目录 .env 中配置 DASHSCOPE_API_KEY 和 AMAP_WEB_KEY")
        return
    print("GroupPilot V0.2.1 已启动。输入完整团建需求，输入 exit 退出。")
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
        observer = StreamObserver()
        try:
            summary = observer.run(agent, request)
        except KeyboardInterrupt:
            print("\n已取消本次任务；已执行的外部查询不会自动撤销。")
            continue
        except Exception as exc:
            print(f"\n执行失败：{type(exc).__name__}。本地 Trace：{observer.trace_path}；请检查 API 及网络配置。")
            continue
        print("\n===== 出行调研结果 =====")
        print(summary.final_text or "尚未捕获有效最终回答，请检查本地 Trace 与程序日志。")
        print(f"\n[执行统计] 模型调用：{summary.model_calls} | 工具调用：{summary.tool_calls} | 工具异常：{summary.tool_failures} | Token(in/out)：{summary.input_tokens}/{summary.output_tokens} | 耗时：{summary.elapsed_seconds}s")
        print(f"[本地 Trace] {summary.trace_path}")
        print("注意：当前每次输入是独立任务；Token 用量以模型实际返回的统计字段为准，可能缺失。")


if __name__ == "__main__":
    main()
