# ScenePilot 后端架构

## 目标

服务人像约拍摄影师，综合模特妆造、服饰、摄影风格、场景类型、光线、天气、日期、交通、预算、器材和场地限制，推荐现实中存在且尽可能可执行的拍摄方案。

**事实与建议分层：** 地图提供地点、坐标和路线，模型提供摄影审美建议；没有实拍图时，不能把某处的视觉氛围当作已验证事实。

## 当前流程

1. CLI 或任务 API 最多一次模型调用提取 `BriefDraft`；用户编辑全部字段并明确确认，宿主构造 `ConfirmedBrief` 不可变快照。缺少城市或风格不能确认。未确认不连接地图开展调研；API 还需显式调用启动接口。
2. 驾车且有地址时，宿主按确认的城市/地址调用受预算保护的 `maps_geo`，展示结果并由用户选 ID。随后 Deep Agents `write_todos` 安排调研任务，`task` 委派 Style Agent 分析审美需求。
3. Scene Agent 调用高德官方 MCP 搜索真实候选地点。
4. 用户已选地图出发位置且交通方式为驾车时，Route Agent 调用高德 MCP 获取驾车路线；查询前拒绝错误起点或非本次候选终点。
5. 主 Agent 对最多三处地图候选调用 `inspect_shooting_conditions`，从本次已保存需求取明确日期，查询区域天气并本地计算太阳时段。
6. 主 Agent 汇总建议并提交证据 ID。后端核对确认编号、出发证据和终点，按秒比较已确认的单程时长上限，附加天气/太阳报告。模型遗漏路线 ID 时也会匹配已有有效路线，避免隐藏已知超时。
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
  路线必须匹配候选地坐标。V0.5.0 的已确认模式还要求起点匹配用户选择的地图定位、确认编号匹配本次需求且交通方式为驾车；旧未确认调用的交通校验只能是 unknown。
- 建议文字仍为模型推断，schema 不保证审美建议的语义正确；用户原话校验为精确子串匹配，不声称完成语义蕴含验证。
  POI 只证明地图服务的返回值，不证明现场状态、许可或照片效果。
- `StreamObserver.arun` 建立/清理运行作用域，180 秒后取消在途异步任务。预算/图步骤耗尽以 `budget_exhausted` 安全停止，
  没有有效提交则返回 `incomplete` 和已取得的证据；未知异常记录类型后继续抛出，外层 CLI 负责提示。
  Trace 仅包含计数、状态和耗时；推荐结构与证据不写入日志。CLI 的结果在内存中；V0.6.0 API 将需求、确认、定位候选与最终结构化结果保存至 SQLite。

## 组件

- `api/app.py`、`models.py`：HTTP 入口和请求/响应契约，确认与位置选择是独立用户操作。
- `api/service.py`：任务状态、并发槽位、等待选择、取消与关闭处理。
- `api/store.py`：SQLite 持久化、原子版本检查、单进程文件锁和重启恢复。
- `api/runtime.py`：懒加载现有模型/地图工具，复用提取器、Agent 图与 Observer，输出只来自证据校验结果。
- `src/scene_pilot/agent.py`：Supervisor。
- `agents/subagents.py`：Style、Scene、Route 工具隔离。
- `integrations/amap_mcp.py`：高德 Streamable HTTP MCP 客户端及工具白名单。
- `core/photography.py`：Pydantic 需求结构。
- `tools/photography_tools.py`：需求校验、现场图片人工核验关键词。
- `observability/streaming.py`：流式执行及本地安全 JSONL Trace。
- `diagnostics.py`：单次模型调用的安全排错工具。

## 下阶段：继续后端

1. 少量受控验收 V0.6.0 HTTP 流程与需求确认/交通时长校验；离线测试不代替真实服务验收。
2. 有可靠来源后再扩展场地费用等规则；目前预算和许可保持未知，不由模型自行判断已满足。
3. 在已实现 REST、SQLite 和离线持续集成基础上增加 SSE；公网部署前另做鉴权和用户数据隔离。
4. 多模态模特妆造/参考照片理解与摄影师确认。
5. 后端稳定后再做前端。

外部 API 有配额与许可限制；不要在单元测试里消耗真实服务次数或泄露凭证。

## V0.6.0 任务生命周期

`extracting → awaiting_confirmation → ready → running → [awaiting_origin → running] → 终态`。
正常提交为 `completed`，证据不足或未提交为 `incomplete`；预算耗尽、失败、取消和中断分别记录。
完成状态不等于所有需求均已满足；业务结论仍以推荐中的各项校验为准。

HTTP 写操作在 SQLite 事务内检查状态和 revision；请求处理器不等待整个规划，后台 asyncio Task 执行。
默认最多 2 个活跃工作者，定位等待通过 Future 将用户选择交还原运行上下文，预算/证据不会因跨 HTTP 请求而重置。
等待上限 900 秒，超时中断且不代选；模型/地图执行时间仍沿用原限制。
取消先保存终态，再取消工作者；Observer 若已取得证据，适配器保存部分结果，晚到结果不能将取消/中断改成完成。

数据库由单进程文件锁保护。服务器启动时只把提取中、执行中或等待定位的任务标为中断，不重放外部调用；
待需求确认和已确认待启动的任务可继续，提取用量与确认编号保留。HTTP 查询只读已保存状态，不触发重试。
当前没有持久化 Agent 检查点、后台队列、跨进程工作者、SSE 或身份系统，接口默认只监听本机。

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
