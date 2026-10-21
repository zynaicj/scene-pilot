"""ScenePilot CLI：通过异步 Deep Agents + MCP 运行人像拍摄选景调研。"""

import asyncio

from scene_pilot.agent import create_planning_graph
from scene_pilot.briefing import extract_brief
from scene_pilot.cli_confirmation import choose_departure, review_brief
from scene_pilot.core.confirmation import confirm_brief
from scene_pilot.core.config import get_settings
from scene_pilot.core.llm import create_qwen_model
from scene_pilot.integrations.amap_mcp import load_amap_tools
from scene_pilot.observability.streaming import StreamObserver


async def run_cli() -> None:
    settings = get_settings()
    if not settings.qwen_api_key or not settings.amap_web_key:
        print("请在项目根目录 .env 中配置 DASHSCOPE_API_KEY 和 AMAP_WEB_KEY")
        return

    print("ScenePilot V0.5.0（人像约拍选景）。先提取需求供你检查，确认后开始选景。")
    print("需求提取最多调用一次模型，计入本次 12 次模型额度；修改字段不调用模型。")
    try:
        model = create_qwen_model(settings)
    except (
        Exception
    ) as exc:  # Constructor errors can also contain private configuration.
        print(f"模型配置初始化失败：{type(exc).__name__}。请检查 .env 配置。")
        return
    agent, geocode_tool = None, None
    print("输入妆造、穿搭、摄影风格、城市等需求，输入 exit 退出。")
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

        observer = None
        try:
            extracted = await extract_brief(model, request)
            if extracted.error_code == "request_too_long":
                print("需求过长，请缩短到 12000 字符以内。")
                continue
            if extracted.error_code:
                print("需求自动提取未完成；可直接手动填写，不自动重试。")
            brief = await asyncio.to_thread(review_brief, extracted.draft)
            if brief is None:
                print("本次已取消，尚未开始地图调研。")
                continue
            confirmed = confirm_brief(
                request, brief, accepted=True, preparation_usage=extracted.usage
            )
            if agent is None:
                print("需求已确认，正在连接高德 MCP……")
                scene_tools, route_tools, weather_tools = await load_amap_tools(
                    settings.amap_web_key
                )
                agent = create_planning_graph(
                    model, scene_tools, route_tools, weather_tools
                )
                geocode_tool = next(
                    (tool for tool in route_tools if tool.name == "maps_geo"), None
                )

            async def select_origin(options):
                return await asyncio.to_thread(choose_departure, options)

            observer = StreamObserver()
            summary = await observer.arun(
                agent,
                request,
                confirmed_brief=confirmed,
                geocode_tool=geocode_tool,
                select_origin=select_origin,
            )
        except (EOFError, KeyboardInterrupt):
            print("\n已取消本次任务；已经发出的 API 请求不会自动撤销。")
            return
        except Exception as exc:  # noqa: BLE001 - CLI privacy boundary
            print(f"执行失败：{type(exc).__name__}。请检查输入或服务连接。")
            if observer is not None:
                print(f"安全 Trace 路径：{observer.trace_path}")
            continue

        print("\n===== ScenePilot 选景建议 =====")
        print(summary.final_text or "未捕获有效最终回答；请检查本地 Trace 和终端事件。")
        print(
            f"\n[运行统计] 模型响应/提取尝试：{summary.model_calls} | 工具请求：{summary.tool_calls} | "
            f"工具错误：{summary.tool_failures} | Token(in/out)：{summary.input_tokens}/{summary.output_tokens} | "
            f"耗时：{summary.elapsed_seconds}s"
        )
        print(f"[安全 Trace] {summary.trace_path}")
        print(
            f"[预算/缓存] 状态：{summary.status} | "
            f"地图尝试：{summary.metrics.get('map_attempts', 0)} | "
            f"缓存命中：{summary.metrics.get('cache_hits', 0)} | "
            f"重试：{summary.metrics.get('map_retries', 0)} | "
            f"缺失用量统计的模型响应：{summary.metrics.get('usage_missing', 0)}"
        )
        print(
            "说明：天气为区域逐日预报，太阳时段为本地天文估算，均不等于现场核验；高德调用受账号额度约束。"
        )


def main() -> None:
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
