"""统一创建 Qwen 模型。"""
from langchain_openai import ChatOpenAI
from group_pilot.core.config import Settings


def create_qwen_model(settings: Settings) -> ChatOpenAI:
    if not settings.qwen_api_key:
        raise RuntimeError("请在 .env 中配置 DASHSCOPE_API_KEY")
    return ChatOpenAI(
        model=settings.qwen_model,
        api_key=settings.qwen_api_key,
        base_url=settings.qwen_base_url,
        temperature=0,
        timeout=90,
        max_retries=2
    )
