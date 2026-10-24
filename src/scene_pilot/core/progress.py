"""Fixed public progress vocabulary; never carry model/tool payloads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

ProgressPhase = Literal[
    "extracting",
    "awaiting_confirmation",
    "ready",
    "loading",
    "locating",
    "planning",
    "style",
    "searching",
    "routes",
    "conditions",
    "finalizing",
    "awaiting_origin",
    "completed",
    "incomplete",
    "budget_exhausted",
    "failed",
    "cancelled",
    "interrupted",
]
PHASE_MESSAGES = {
    "extracting": "正在整理摄影需求",
    "awaiting_confirmation": "请检查并确认摄影需求",
    "ready": "需求已确认，可以开始规划",
    "loading": "正在准备规划服务",
    "locating": "正在查询出发位置",
    "planning": "正在规划拍摄方案",
    "style": "正在分析拍摄风格",
    "searching": "正在查找候选拍摄地点",
    "routes": "正在查询候选地点的交通路线",
    "conditions": "正在检查天气与光照条件",
    "finalizing": "正在校验并整理最终方案",
    "awaiting_origin": "请选择实际出发位置",
    "completed": "拍摄方案已生成，请查看各项条件是否满足",
    "incomplete": "规划未完整完成，可查看已取得的信息",
    "budget_exhausted": "已达到本次执行限制，可查看已取得的信息",
    "failed": "本次规划失败，请查看错误提示",
    "cancelled": "任务已取消",
    "interrupted": "任务已中断，不会自动重新规划",
}


class TaskProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    phase: ProgressPhase = "extracting"

    @computed_field
    @property
    def message(self) -> str:
        return PHASE_MESSAGES[self.phase]


def progress_from_event(event, fields):
    if event == "subagent_delegated":
        phase = {
            "style-agent": "style",
            "scene-agent": "searching",
            "route-agent": "routes",
        }.get(fields.get("subagent"))
    elif event == "tool_requested":
        phase = {
            "write_todos": "planning",
            "maps_text_search": "searching",
            "maps_around_search": "searching",
            "maps_search_detail": "searching",
            "maps_direction_driving": "routes",
            "inspect_shooting_conditions": "conditions",
            "finalize_recommendation": "finalizing",
        }.get(fields.get("tool"))
    else:
        phase = None
    return TaskProgress(phase=phase) if phase else None
