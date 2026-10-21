# ScenePilot 项目协作说明

给 ChatGPT Work、IDE Agent 和开发者：继续任务时先阅读本文件、`README.md`、`docs/ARCHITECTURE.md` 与最近 Git 提交。

## 目标与约束

- 产品为**人像约拍选景与拍摄策划的 Python 后端**；先做业务 API，前端放后期。
- 技术栈：Python 3.12、uv、Deep Agents、Qwen、Pydantic、高德官方 MCP。
- `src/scene_pilot/` 是唯一正式包，不再引入旧团建业务或旧包名。
- 使用 Deep Agents 的 `write_todos` 和 `task`；按职责给 Subagents 限定工具。
- 地图坐标/路线不意味着摄影现场已核验；未经验证的票价、许可、构图不能被描述成确定事实。
- 缺少实拍证据时生成小红书/抖音的人工搜索关键词，不爬取或声称看过图片。
- 外部工具优先选有合法免费额度的 MCP 服务，并遵守非商业和配额限制。
- 单元测试必须不调用付费模型或真实地图。
- 不提交 `.env`、任何 API Key、隐私照片或敏感日志。
- 用户希望代码风格整洁：复杂 Agent 配置、字典、invoke/stream 调用多行排版，短代码保持紧凑。
- 修改前核对 GitHub 最新 `main`，优先分支开发，验收通过再提交；报告新增、修改、删除文件及 Commit。

## 验证命令

```powershell
uv sync
uv run python -m unittest discover -s tests -v
uv run scene-pilot-check-model
uv run scene-pilot
```

其中模型诊断和 ScenePilot CLI 会实际调用外部服务，可能消耗模型及地图额度。单元测试完全离线。

## 当前版本和后续

V0.5.0：CLI 先用最多一次模型调用提取草稿，用户逐项修改并明确确认；提取计入原有 12 次模型预算。`confirm_brief` 仅供取得用户接受的宿主调用，不得暴露给模型。地图定位后的出发位置也须用户选择，定位计入 8 次地图总预算。已确认模式在查询和最终提交时绑定起点/终点/驾车方式/确认编号，由本地代码按秒比较单程时长并输出 meets/not_met/unknown；遗漏路线引用也不能隐藏已取得的有效路线。旧未确认调用兼容但不能给出符合硬约束的结论。用户等待不计入规划截止时间；提取原文、确认详情和定位地址不写入安全 Trace。验收见 `docs/V0.5.0.md`。下一步为受控验收、FastAPI / SSE / SQLite，多模态与前端仍靠后。

V0.4.0：在 V0.3.1 的缓存/预算/证据输出上增加可选高德天气 MCP 与本地 Astral 太阳时段。需求保存在本次 RunContext；日期仅接受用户原文中的 YYYY-MM-DD，不将其他模型提取字段视为已确认硬约束。天气缺失发布时间标为 reference，不用查询时间代替发布时间。天气最多 2 次尝试，计入高德总预算 8 次。生产通过 `StreamObserver.arun` 运行，不能将 graph 自由文本直接作为事实输出。验收说明见 `docs/V0.4.0.md`。后续依次推进需求确认与本地业务硬约束校验、FastAPI / SSE / SQLite、多模态、前端。

历史上出现过 Qwen 免费额度 403 和模型服务 500。开发时不要反复消耗配额，先跑离线单测，再少量做端到端验证。
