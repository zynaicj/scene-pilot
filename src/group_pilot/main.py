"""ScenePilot CLI：通过异步 Deep Agents + MCP 运行人像拍摄选景调研。"""
import asyncio
from group_pilot.agent import build_scene_pilot_agent
from group_pilot.core.config import get_settings
from group_pilot.observability.streaming import StreamObserver


async def run_cli() -> None:
    settings = get_settings()
    if not settings.qwen_api_key or not settings.amap_web_key:
        print("请在项目根目录 .env 中配置 DASHSCOPE_API_KEY 和 AMAP_WEB_KEY")
        return

    print("ScenePilot V0.3（人像约拍选景）。正在连接高德 MCP，请稍候……")
    try:
        agent = await build_scene_pilot_agent()
    except Exception as exc:
        # MCP 请求 URL 包含 Key，不打印原始异常文本。
        print(f"初始化失败：{type(exc).__name__}。请检查网络、MCP 连接和 .env 配置。")
        return

    print("就绪。输入妆造、穿搭、摄影风格、城市等需求，输入 exit 退出。")
    while True:
        try:
            request = input("\n拍摄需求 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            return
        if request.lower() in {"exit", "quit", "退出"}:
            return
        if not request:
            continue

        observer = StreamObserver()
        try:
            summary = await observer.arun(agent, request)
        except KeyboardInterrupt:
            print("\n已取消本次任务；已经发出的 API 请求不会自动撤销。")
            continue
        except Exception as exc:
            print(f"执行失败：{type(exc).__name__}。安全 Trace 路径：{observer.trace_path}")
            continue

        print("\n===== ScenePilot 选景建议 =====")
        print(summary.final_text or "未捕获有效最终回答；请检查本地 Trace 和终端事件。")
        print(
            f"\n[运行统计] 模型消息：{summary.model_calls} | 工具请求：{summary.tool_calls} | "
            f"工具错误：{summary.tool_failures} | Token(in/out)：{summary.input_tokens}/{summary.output_tokens} | "
            f"耗时：{summary.elapsed_seconds}s"
        )
        print(f"[安全 Trace] {summary.trace_path}")
        print("说明：未接入实拍图核验、精准天气或太阳位置计算；地图调用量受高德账号额度约束。")


def main() -> None:
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
