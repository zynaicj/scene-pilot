"""Local CLI review; acceptance and origin selection are never delegated to AI."""

from scene_pilot.core.confirmation import BriefDraft
from scene_pilot.core.photography import PhotographyBrief

BRIEF_LABELS = {
    "city": "拍摄城市（必填）",
    "photography_style": "摄影风格（必填）",
    "departure": "出发地址（请填写具体地点）",
    "shooting_date": "拍摄日期（YYYY-MM-DD）",
    "clothing": "服饰",
    "makeup": "妆造",
    "desired_scenes": "目标场景（逗号分隔）",
    "lighting": "光线",
    "reference_description": "参考照片文字描述",
    "transport_mode": "交通方式（目前仅支持驾车路线）",
    "max_travel_minutes": "单程时长上限（分钟）",
    "max_budget_cny": "总预算上限（元）",
    "equipment": "器材（逗号分隔）",
    "crew_size": "团队人数",
    "venue_requirements": "场地要求（逗号分隔）",
}
LIST_FIELDS = {"desired_scenes", "equipment", "venue_requirements"}


def review_brief(
    draft: BriefDraft, *, read=input, emit=print
) -> PhotographyBrief | None:
    """Editing invalidates the preview; only a later explicit confirmation exits."""
    values = draft.model_dump(mode="json")
    fields = list(BRIEF_LABELS)
    while True:
        emit("\n请检查需求草稿；未知项保持未填写。")
        for index, name in enumerate(fields, 1):
            value = values.get(name)
            if isinstance(value, list):
                value = "、".join(value)
            shown = "未填写" if value in (None, "") else str(value)
            emit(f"{index}. {BRIEF_LABELS[name]}：{shown}")
        command = read(
            "输入编号修改，输入「确认」开始选景，输入「取消」返回 > "
        ).strip()
        if command in {"取消", "exit", "quit", "退出"}:
            return None
        if command == "确认":
            try:
                return PhotographyBrief.model_validate(values)
            except ValueError:
                emit("尚不能确认：请填写城市和摄影风格，并检查字段格式。")
                continue
        if not command.isdecimal() or not 1 <= int(command) <= len(fields):
            emit("请输入有效编号，或明确输入「确认」「取消」。")
            continue
        name = fields[int(command) - 1]
        raw = read(f"{BRIEF_LABELS[name]}（输入「清空」删除） > ").strip()
        if raw == "清空" or not raw:
            value = [] if name in LIST_FIELDS else None
        elif name in LIST_FIELDS:
            value = [
                part.strip()
                for part in raw.replace("，", ",").split(",")
                if part.strip()
            ]
        else:
            value = raw
        try:
            edited = BriefDraft.model_validate({**values, name: value})
        except ValueError:
            emit(
                "格式无效，原值已保留。日期使用 YYYY-MM-DD；时长、人数和预算填写有效数字。"
            )
            continue
        values = edited.model_dump(mode="json")


def choose_departure(options, *, read=input, emit=print) -> str | None:
    emit(
        "\n地图返回以下出发位置，请确认实际出发点；地址过于宽泛或位置不正确时请选择跳过。"
    )
    for index, item in enumerate(options, 1):
        emit(f"{index}. {item.formatted_address or '地址未知'}；坐标 {item.location}")
    while True:
        value = read(
            "输入位置编号确认，或输入「跳过」（路线时长将保持未知） > "
        ).strip()
        if value in {"跳过", "取消", "exit", "quit", "退出"}:
            return None
        if value.isdecimal() and 1 <= int(value) <= len(options):
            return options[int(value) - 1].evidence_id
        emit("请明确选择编号或输入「跳过」。")
