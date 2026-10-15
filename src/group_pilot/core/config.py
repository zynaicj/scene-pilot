"""运行配置，仅从环境变量读取密钥。"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    qwen_api_key: str
    qwen_model: str
    qwen_base_url: str
    amap_web_key: str


def get_settings() -> Settings:
    load_dotenv()
    return Settings(qwen_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(), qwen_model=os.getenv("QWEN_MODEL", "qwen-plus"), qwen_base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"), amap_web_key=os.getenv("AMAP_WEB_KEY", "").strip())
