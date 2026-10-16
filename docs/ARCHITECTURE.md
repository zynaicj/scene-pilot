# ScenePilot 后端架构

## 目标

服务人像约拍摄影师，综合模特妆造、服饰、摄影风格、场景类型、光线、天气、日期、交通、预算、器材和场地限制，推荐现实中存在且尽可能可执行的拍摄方案。

**事实与建议分层：** 地图提供地点、坐标和路线，模型提供摄影审美建议；没有实拍图时，不能把某处的视觉氛围当作已验证事实。

## 当前流程

1. 主 Agent 调用 `prepare_photography_brief` 验证可用输入；未知条件保持为空。
2. Deep Agents `write_todos` 安排调研任务，`task` 委派 Style Agent 分析审美需求。
3. Scene Agent 调用高德官方 MCP 搜索真实候选地点。
4. 有准确出发地时，Route Agent 调用高德 MCP 获取驾车路线。
5. 主 Agent 对最多三处地图候选调用 `inspect_shooting_conditions`，从本次已保存需求取明确日期，查询区域天气并本地计算太阳时段。
6. 主 Agent 汇总摄影建议、地图事实、预算/场地未知因素和社交媒体实拍核验关键词。最终方案由后端附加已登记的天气/太阳报告。
7. StreamObserver 记录执行元数据；敏感输入、API Key 不写入 Trace。

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

1. 用户确认结构化需求，建立可用于硬约束判断的可信输入。
2. 场地费用/路线/时间等业务硬约束校验（运行调用预算和基础结构化结果已在 V0.3.1 完成；天气/太阳时段已在 V0.4.0 完成）。
3. FastAPI REST/SSE API、SQLite 状态持久化和持续集成。
4. 多模态模特妆造/参考照片理解与摄影师确认。
5. 后端稳定后再做前端。

外部 API 有配额与许可限制；不要在单元测试里消耗真实服务次数或泄露凭证。

## V0.4.0 天气与太阳时段

- `load_amap_tools` / `select_amap_tools` 返回地点、路线、天气三个工具列表，天气可为空。
  天气原始工具只绑定到本地 `inspect_shooting_conditions` 实现，不直接暴露给模型或子 Agent。
- 本次 `RunContext` 保存需求、天气证据、按 POI 编号索引的条件报告；不会跨请求共享。
  需求首次保存后不可在本次运行中更改；日期需精确出现在用户原文中。该校验不证明用户语义或其他需求字段已确认。
- 地图投影增加 `cityname` / `adcode`。天气优先按地图返回城市查询；缺失时按 adcode 查询，返回地区仍须核对。
  两者都缺失时可使用明确记录的搜索 `city` 参数作为查询上下文，单独保存在 `query_city`，不冒充 POI 返回城市。
  同一 POI 的详情可继承本次唯一的搜索城市；存在多个搜索城市时不继承。此路径仅提供 `reference`，标注 POI 行政归属未核实。
  支持标准 `forecasts[].casts[]` 和真实 MCP 的 `{city, forecasts:[每日字段]}` 格式；未知格式失败关闭。
  未返回的发布时间/adcode 不补造。发布时间缺失时报告 `reference`；过期、地区不一致、日期不覆盖时报告 `unknown`。
- 本地计算基于 Astral 3.2，单独校正黄金时段两个端点的北京时间日期，避免其 UTC transit 落到相邻日。
  日出、日落及两个黄金时段分别计算；不存在某天文事件时保留其他结果，所有输出标为估算。
- 天气调用复用串行传输、25 秒超时、最多一次传输重试、缓存和总预算，另限天气尝试最多 2 次。
  条件查询最多累计处理三处 POI，同一 POI 复用结果；本次失败保留为未知，不循环重试。
  先保存本地太阳报告再等待天气，取消或超时时可通过 Observer 的部分结果保留已有计算。
