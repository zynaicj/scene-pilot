# ScenePilot 后端架构

## 目标

服务人像约拍摄影师，综合模特妆造、服饰、摄影风格、场景类型、光线、天气、日期、交通、预算、器材和场地限制，推荐现实中存在且尽可能可执行的拍摄方案。

**事实与建议分层：** 地图提供地点、坐标和路线，模型提供摄影审美建议；没有实拍图时，不能把某处的视觉氛围当作已验证事实。

## 当前流程

1. 主 Agent 调用 `prepare_photography_brief` 验证可用输入；未知条件保持为空。
2. Deep Agents `write_todos` 安排调研任务，`task` 委派 Style Agent 分析审美需求。
3. Scene Agent 调用高德官方 MCP 搜索真实候选地点。
4. 有准确出发地时，Route Agent 调用高德 MCP 获取驾车路线。
5. 主 Agent 汇总摄影建议、地图事实、预算/场地未知因素和社交媒体实拍核验关键词。
6. StreamObserver 记录执行元数据；敏感输入、API Key 不写入 Trace。

## 组件

- `src/scene_pilot/agent.py`：Supervisor。
- `agents/subagents.py`：Style、Scene、Route 工具隔离。
- `integrations/amap_mcp.py`：高德 Streamable HTTP MCP 客户端及工具白名单。
- `core/photography.py`：Pydantic 需求结构。
- `tools/photography_tools.py`：需求校验、现场图片人工核验关键词。
- `observability/streaming.py`：流式执行及本地安全 JSONL Trace。
- `diagnostics.py`：单次模型调用的安全排错工具。

## 下阶段：继续后端

1. 免费额度天气 MCP 与本地太阳位置（Astral）计算。
2. 结构化场景推荐结果、预算/路线/时间硬约束校验与工具调用预算治理。
3. FastAPI REST/SSE API、SQLite 状态持久化和持续集成。
4. 多模态模特妆造/参考照片理解与摄影师确认。
5. 后端稳定后再做前端。

外部 API 有配额与许可限制；不要在单元测试里消耗真实服务次数或泄露凭证。
