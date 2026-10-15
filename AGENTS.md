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

V0.3.1：保留 V0.3 摄影需求、Style/Scene/Route Agent、高德 MCP、人工实拍关键词；新增按次运行的缓存/去重/预算、来源记录、结构化推荐与安全停止。生产通过 `StreamObserver.arun` 运行，不能将 graph 自由文本直接作为事实输出。验收说明见 `docs/V0.3.1.md`。后续依次推进天气 MCP + 太阳位置、本地业务硬约束校验、FastAPI / SSE / SQLite、多模态、前端。

历史上出现过 Qwen 免费额度 403 和模型服务 500。开发时不要反复消耗配额，先跑离线单测，再少量做端到端验证。
