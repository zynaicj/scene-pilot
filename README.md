# ScenePilot · 光影选景师

**基于 Deep Agents、Qwen 和高德官方 MCP 的智能人像约拍场景规划后端。**

摄影师输入模特妆造、服饰、摄影风格、场景偏好、自然光线、预算和交通限制，ScenePilot 分析创作意图、调研真实拍摄地点及可查询的驾车路线，并提供拍摄建议与人工实拍核验关键词。

当前版本：**V0.3（后端）**。暂不开发前端，未来将增加 FastAPI 接口、持久化和多模态图片理解。

## 功能

- Deep Agents 主 Agent：`write_todos` 任务拆解与 `task` 子 Agent 委派。
- Style Agent：分析文字描述的妆造、服饰、摄影风格、背景与光线需求。
- Scene Agent：通过高德官方 MCP 检索真实 POI。
- Route Agent：已知出发地时调用 MCP 查询单程驾车路线。
- Pydantic `PhotographyBrief`：结构化地点、风格、日期、预算、器材、团队和场地条件。
- 实拍核验工具：生成适合在小红书、抖音等平台**人工搜索**的关键词，不抓取网站、不声称已核验实拍图片。
- 流式执行与安全 Trace：记录工具事件、调用次数和模型可用的 Token 统计，不保存用户原始输入及密钥。

## 本地环境

需要 Python 3.12、uv、可用的百炼 Qwen API Key，以及在高德开放平台申请的 Web 服务 Key。

```powershell
uv sync
```

如果根目录没有 `.env`，可从 `.env.example` 复制；已经有 `.env` 的用户**不要覆盖**原有文件：

```dotenv
DASHSCOPE_API_KEY=仅在本地填写
QWEN_MODEL=填写你当前能正常调用的模型
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AMAP_WEB_KEY=仅在本地填写
```

不要上传真实 API Key、含密钥的截图或者完整敏感错误 URL。

先做一次真实模型连通性诊断（可能消耗模型额度）：

```powershell
uv run scene-pilot-check-model
```

运行无需外部密钥的单元测试：

```powershell
uv run python -m unittest discover -s tests -v
```

运行摄影选景：

```powershell
uv run scene-pilot
```

也可以运行 `uv run python -m scene_pilot.main`。

输入例子：

> 我想在杭州拍日系清新写真，模特穿白色连衣裙，淡妆自然披发，希望有树林、草地和柔和逆光。从下沙出发，单程驾车不超过 40 分钟，预算 300 元。请寻找真实候选拍摄地点并给出实拍核验关键词。

## 工程目录

```text
src/scene_pilot/
  agent.py                   # Photography Supervisor
  agents/subagents.py        # Style / Scene / Route 子 Agent
  core/photography.py        # 摄影需求数据模型
  core/config.py             # 从 .env 加载运行配置
  core/llm.py                # Qwen 连接
  integrations/amap_mcp.py   # 高德官方 MCP
  tools/photography_tools.py # 需求与实拍核验工具
  observability/streaming.py # 安全流式执行 Trace
  diagnostics.py             # 单次模型连通性诊断
  main.py                    # CLI
tests/                       # 无 Key 单元测试
docs/ARCHITECTURE.md         # 架构及下一步路线
AGENTS.md                    # Work / 开发者项目说明
```

## 费用与边界

高德地图受个人账户免费额度和使用许可约束；MCP 是通信协议，不等于第三方接口永久免费。Qwen 模型是否计费取决于所用模型及百炼账户的可用额度。

当前只理解**文字描述**，不能实际识别模特照片或参考图片；也没有接入实时天气、太阳位置计算、场地商业拍摄许可、真实票价、数据库或 HTTP API。

地点名称与路线可能来自地图接口，但**不能据此保证某处具备期待的机位、植被、光线或免费开放**。没有现场图像证据时，建议摄影师在社交媒体搜索近期实拍图进行确认。

更多设计见 `docs/ARCHITECTURE.md`。
