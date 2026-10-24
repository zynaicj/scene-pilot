"""Public messages are fixed local text, independent of provider exception content."""

ERROR_MESSAGES = {
    "invalid_request": "请求参数不正确，请检查填写内容。",
    "request_too_large": "请求内容过大，请缩短后再提交。",
    "request_too_long": "需求内容过长，请精简后再提交。",
    "task_not_found": "未找到该任务，请刷新任务列表。",
    "revision_conflict": "任务内容或状态已更新，请刷新后重新确认。",
    "invalid_task_state": "当前任务状态不支持此操作，请刷新查看。",
    "idempotency_key_conflict": "此次提交标识已用于其他需求，请为新需求使用新标识。",
    "complete_brief_required": "请填写拍摄城市和摄影风格后再确认。",
    "confirmation_required": "请先检查并确认摄影需求。",
    "origin_not_in_this_task": "请选择当前任务提供的出发位置。",
    "origin_selection_not_pending": "当前没有等待选择的出发位置，请刷新任务。",
    "origin_selection_expired": "等待选择出发位置已超时，任务已中断；如需继续请新建任务。",
    "result_not_ready": "任务尚未结束，请稍后查看结果。",
    "server_busy": "正在处理的任务已达上限，请稍后手动重试。",
    "server_stopping": "服务正在关闭，请稍后重新连接。",
    "server_stopped": "服务已停止，任务已中断；如需继续请新建任务。",
    "server_restarted": "服务已重启，原任务已中断；如需继续请新建任务。",
    "model_configuration_required": "模型配置不可用，可以先手动填写需求。",
    "brief_extraction_failed": "自动整理需求失败，请手动检查并补全草稿。",
    "extraction_failed": "整理需求时发生错误，任务已停止。",
    "planning_failed": "规划服务发生错误，可查看已取得的信息。",
    "invalid_event_cursor": "进度记录标识不正确，请重新连接当前任务。",
    "event_stream_limit": "进度连接已达上限，请关闭多余页面后重试。",
    "event_stream_unavailable": "进度连接暂时不可用，请重新连接或查询任务详情。",
    "http_error": "请求无法处理，请检查接口地址和请求方式。",
    "internal_error": "服务暂时无法处理请求，请稍后查看任务状态。",
}


def error_message(code: str | None) -> str | None:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal_error"]) if code else None


def error_payload(code: str) -> dict:
    safe_code = code if code in ERROR_MESSAGES else "internal_error"
    return {"error_code": safe_code, "message": error_message(safe_code)}
