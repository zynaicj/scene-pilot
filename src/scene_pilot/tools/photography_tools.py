"""摄影需求校验和场景人工核验建议，均为本地免费工具。"""

from langchain_core.tools import tool
from scene_pilot.core.execution import CURRENT_RUN
from scene_pilot.core.photography import PhotographyBrief, make_verification_queries


@tool(args_schema=PhotographyBrief)
def prepare_photography_brief(**kwargs) -> dict:
    """构造并验证结构化人像约拍需求。未知的日期、价格和场地条件应保持为空，不得猜测。"""
    brief = PhotographyBrief.model_validate(kwargs)
    context = CURRENT_RUN.get()
    if context is not None:
        if (
            brief.shooting_date
            and brief.shooting_date.isoformat() not in context.request
        ):
            return {
                "status": "error",
                "error_code": "explicit_date_required",
                "message": "日期须来自用户原文中的 YYYY-MM-DD；相对日期请留空并请用户补充。",
            }
        if context.brief is not None and context.brief != brief:
            return {
                "status": "error",
                "error_code": "brief_already_prepared",
                "message": "本次需求已保存；更改条件请开始新一轮规划。",
            }
        context.brief = brief.model_copy(deep=True)
    return {
        "status": "success",
        "brief": brief.model_dump(mode="json", exclude_none=True),
        "note": "该工具只校验输入，不代表已经验证天气、真实场景或拍摄许可。",
    }


@tool
def recommend_scene_verification(place_name: str, photography_style: str) -> dict:
    """针对已检索到的真实地点，生成小红书/抖音等平台的人工实拍核验搜索词，不抓取数据。"""
    try:
        queries = make_verification_queries(place_name, photography_style)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    return {
        "status": "success",
        "verification_state": "unverified",
        "queries": queries,
        "advice": "请到小红书、抖音等平台自行查看近期真实照片，核对季节、机位、人流、开放时间、换装条件及拍摄许可。",
    }
