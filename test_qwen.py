
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

model = ChatOpenAI(
    model=os.environ["QWEN_MODEL"],
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url=os.environ["QWEN_BASE_URL"],
    temperature=0,
)

response = model.invoke(
    "你好，我正在开发 GroupPilot 团体出行规划 Agent，请简单介绍一下你自己。"
)

print(response.content)
