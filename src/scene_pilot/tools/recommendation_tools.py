"""Resolve model selections to server-held evidence before final delivery."""

from langchain_core.tools import tool

from scene_pilot.core.execution import current_run
from scene_pilot.core.recommendation import (
    CandidateDraft,
    UserStatement,
    assemble_recommendation,
)


@tool
def finalize_recommendation(
    user_statements: list[UserStatement],
    candidates: list[CandidateDraft],
) -> dict:
    """提交最终方案：用户信息仅传原话，地点仅传 MCP evidence_id 和 AI 建议。最多三地。"""
    context = current_run()
    try:
        plan = assemble_recommendation(
            context.request,
            user_statements,
            candidates,
            context.evidence,
            context.routes,
            context.conditions,
        )
    except ValueError:
        return {
            "status": "error",
            "error_code": "evidence_validation_failed",
            "message": "请使用本次工具返回的 evidence_id、用户原话和不重复的最多三处地点。",
        }
    context.recommendation = plan
    return {"status": "success", "candidate_count": len(plan.candidates)}
