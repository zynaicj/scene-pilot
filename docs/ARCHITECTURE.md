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

## V0.3.1 运行边界

- Supervisor 仍使用 `create_deep_agent`、`TodoListMiddleware.write_todos` 和 Deep Agents `SubAgentMiddleware.task`。
  三类专家以正式的 `CompiledSubAgent` 接入，内部使用 LangChain `create_agent`，只绑定各自工具。
  Supervisor 替换默认任务中间件，移除隐式通用子 Agent；模型请求中间件仅提供业务工具和精简提示词，并在执行时拒绝未授权工具。
- 官方 `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` 限制各图调用；共享 `RunBudgetMiddleware` 通过正式 model wrap hooks
  在发起调用前原子占用全局额度，响应后统计 usage。`ContextVar` 将运行对象传递给本次异步委派及线程上下文，避免用户之间共享状态。
  短任务关闭自动摘要模型调用；使用 LangGraph `recursion_limit` 作为最后的循环保护。
- `amap_mcp.py` 负责串行化、有限传输重试、25 秒超时及错误脱敏；ToolCall 保留 MCP 状态和 structured artifact。
  `amap_policy.py` 负责本次请求内 single-flight 缓存与指标；`amap_results.py` 只投影受支持的完整基本字段，不截断事实字符串。
  最多向模型返回 6 条不重复 POI，并报告 `omitted_count`；异常、过长和未知格式失败关闭。
  缓存键包含工具名及完整规范 JSON 参数，城市/半径不同不会混用。重试在每次上游请求前占用额度，失败不缓存。
- `core/recommendation.py` 定义来源模型；`finalize_recommendation` 只接受用户原话和证据引用，校验引用属于本次运行并拒绝重复地点。
  后端从证据记录构造地点事实、路线、未知字段及人工核验关键词；CLI 从结构化结果确定性渲染，不使用 Supervisor 的自由文本作为最终事实。
  路线必须匹配候选地坐标；查询起点与实际出发位置的关系仍需用户确认。
- 建议文字仍为模型推断，schema 不保证审美建议的语义正确；用户原话校验为精确子串匹配，不声称完成语义蕴含验证。
  POI 只证明地图服务的返回值，不证明现场状态、许可或照片效果。
- `StreamObserver.arun` 建立/清理运行作用域，180 秒后取消在途异步任务。预算/图步骤耗尽以 `budget_exhausted` 安全停止，
  没有有效提交则返回 `incomplete` 和已取得的证据；未知异常记录类型后继续抛出，外层 CLI 负责提示。
  Trace 仅包含计数、状态和耗时；推荐结构与证据只在内存中保留，不写入日志。

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
2. 场地费用/路线/时间等业务硬约束校验（运行调用预算和基础结构化结果已在 V0.3.1 完成）。
3. FastAPI REST/SSE API、SQLite 状态持久化和持续集成。
4. 多模态模特妆造/参考照片理解与摄影师确认。
5. 后端稳定后再做前端。

外部 API 有配额与许可限制；不要在单元测试里消耗真实服务次数或泄露凭证。
