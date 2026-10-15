# ScenePilot · 光影选景师

基于 **Deep Agents + Qwen + 高德地图官方 MCP** 的人像约拍摄影场景调研与拍摄策划系统。前身为 GroupPilot 团体出行 Agent，沿用其多 Agent 编排与可观测性代码，目前为 **V0.3 开发分支**。

> 定位：摄影师根据模特妆造、服饰、目标拍摄风格、参考照片文字描述、场景、光线、预算和地理约束，搜索真实候选地点、核对驾车路线，获得拍摄建议与需人工确认的事项。

## 当前支持

- Deep Agents 主 Agent：`write_todos` 规划任务、`task` 委派专业子 Agent。
- Style Agent：理解**文字描述**中的妆造、服饰、风格与光线需求。
- Scene Agent：通过官方高德地图 MCP 地点检索；Route Agent 负责可选的驾车路线查询。
- Pydantic `PhotographyBrief`：对城市、风格、预算、时间、拍摄团队、设备与场地偏好进行结构化验证。
- 手动实拍核验：对未知现场视觉效果，给出小红书、抖音等社交平台可复制的搜索词，**不爬取、不声称核实过实拍图**。
- LangGraph v2 异步流式进度、元数据 Trace 和离线单元测试。

尚未支持照片上传和实际图像识别；未接入准确天气预报、黄金时刻计算、现场实拍图、门票与场地收费、商业摄影许可数据库、持久化会话。相关信息不得凭空确认。

## 本地启动（Windows PyCharm / Python 3.12 / uv）

1. 在高德开放平台申请**Web 服务 Key**，并在控制台确认可使用个人开发阶段的免费额度。
2. 在项目根目录保留本地 `.env`，配置 `DASHSCOPE_API_KEY`、`QWEN_MODEL`、`QWEN_BASE_URL`、`AMAP_WEB_KEY`。不要提交 `.env`，旧 Key 如曾出现在截图应轮换。
3. 首次拉取 V0.3 开发分支后运行 `uv sync`（自动解析新增 MCP 适配器依赖并更新本地 `uv.lock`）。
4. 执行 `uv run python -m unittest discover -s tests -v`，本地测试不需要真实密钥。
5. 运行 `uv run group-pilot`，终端标题为 ScenePilot；Python 运行入口仍保持兼容旧命令。

示例输入：

> 在杭州拍日系清新写真，模特穿白色棉麻连衣裙，淡妆、披发，希望树林、草地和柔和逆光；从浙江理工大学下沙校区出发，单程驾车最好不超过 40 分钟，预算 300 元。请搜索真实候选地点并提供现场核验关键词。

## MCP 与费用说明

使用 [高德官方 MCP Server](https://developer.amap.com/api/mcp-server/gettingstarted)，通过 Streamable HTTP 连接：`https://mcp.amap.com/mcp?key=...`。密钥**只在本地运行时拼装，绝不写进代码、终端日志或 Trace**。

客户端采用 `langchain-mcp-adapters` 的 `MultiServerMCPClient`。仅允许检索与路线所需工具：`maps_text_search`、`maps_around_search`、`maps_search_detail`、`maps_geo` 和 `maps_direction_driving`。连接失败时停止，不会自动使用假地图数据。

**MCP 协议不等于免费 API**：高德服务在账号许可和免费额度范围内可免费使用，Qwen 模型调用费用取决于你的百炼账户和模型套餐。请遵守外部服务商政策。

## 核心代码

- `src/group_pilot/core/photography.py`：摄影需求数据模型及人工核验关键词。
- `src/group_pilot/agent.py`：摄影策划 Supervisor。
- `src/group_pilot/agents/subagents.py`：Style、Scene、Route 子 Agent。
- `src/group_pilot/integrations/amap_mcp.py`：官方高德 MCP 客户端和工具白名单。
- `src/group_pilot/tools/photography_tools.py`：结构化需求工具和人工核验工具。
- `src/group_pilot/observability/streaming.py`：异步流式执行、工具和 Token 统计。
- `src/group_pilot/main.py`：命令行入口。

`logs/` 和 `.env` 均被 Git 忽略。Trace 保存执行元数据而非用户原始需求。外部 MCP 真实连通性与 Qwen 联调需要在你本机配置密钥后验收。

## 开发状态

V0.3 首先解决**文本摄影意图 → 真实地图选景 → 合理建议和人工核验**这一闭环；后续添加免费天气 MCP、本地 Astral 黄金时刻工具、预算/时间确定性校验和多模态参考图片理解。
