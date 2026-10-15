# GroupPilot · 同行智策

基于 **Deep Agents + Qwen** 的多智能体团体出行规划项目。当前 V0.2 提供目的地 POI 调研、驾车路径查询和主 Agent 对专业 Subagent 的任务委派。

> 项目处于开发阶段。地图数据来自高德 Web 服务 API，暂不提供可核验门票价格、酒店/包车报价、天气检查、付款或实际预订功能；不能将输出视为已经核验的可执行行程。

## 环境准备

要求 Python 3.12、uv，并在高德开放平台（https://developer.amap.com/）申请 Web 服务 Key。Qwen 使用支持工具调用的阿里云百炼 OpenAI 兼容模型。

1. 在项目目录执行：uv sync
2. 从 .env.example 复制一份 .env，在本地填入 DASHSCOPE_API_KEY 和 AMAP_WEB_KEY。
3. 启动命令：uv run group-pilot
4. 或者运行：uv run python -m group_pilot.main

.env 被 Git 忽略，不要提交真实密钥。若密钥曾出现在截图或仓库中，请到服务平台轮换旧密钥。

输入包含出发城市、准确出发地点、日期、人数、预算和偏好的团建需求，例如：

> 12 人周六从杭州某校区出发，一日团建，总预算 3000 元，不希望高强度徒步；先查询候选地点与驾车单程耗时。

## 目录结构

- src/group_pilot/agent.py：Supervisor、Todo Planning、Subagent 委派和预算工具。
- src/group_pilot/agents/subagents.py：目的地与路线专业子 Agent，工具隔离。
- src/group_pilot/tools/：LLM 工具接口，将业务错误返回给模型。
- src/group_pilot/integrations/amap.py：高德地点搜索和驾车路线适配。
- src/group_pilot/core/：环境配置和 Qwen 模型初始化。
- src/group_pilot/main.py：正式命令行入口；每次任务独立，暂未实现持久化对话。

## 本地单元测试

执行：uv run python -m unittest discover -s tests -v

测试模拟高德响应，不消耗 API 配额。后续计划增加结构化需求、多人硬约束、预算明细、状态持久化和动态重规划。


## V0.2.1：流式进度与安全 Trace

输入需求后，命令行会在 Agent 执行期间逐步输出 Supervisor / Subagent 的工具委派和执行进度，不再等整个任务结束后才显示信息。

`uv run group-pilot`

运行时自动创建 `logs/agent_runs/<run_id>.jsonl`，记录工具名称、执行阶段、粗略耗时、模型调用次数及模型提供的 Token 用量；**不会写入模型对话、工具原始参数、原始结果和 API Key**。日志目录被 Git 忽略。工具返回无结构化状态时记录为 `executed`，不等价于业务成功。实际使用时请注意不要将包含隐私的终端输出公开分享。

`uv run python -m unittest discover -s tests -v`

流式测试基于模拟事件，真实 Qwen + 高德流式联调需在本地完成。当前记录的工具耗时来自流式事件之间的时间差，可能包含调度延迟，不能完全代表第三方 API 延迟。暂未实现跨轮持久化与结构化行程。
