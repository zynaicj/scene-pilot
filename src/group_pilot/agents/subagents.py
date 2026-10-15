"""职责明确且工具隔离的 Deep Agents 子智能体。"""
from langchain_core.language_models import BaseChatModel
from group_pilot.tools.map_tools import locate_departure, query_driving_route, search_destinations


def build_subagents(model: BaseChatModel) -> list[dict]:
    return [
        {
            "name": "destination-agent",
            "description": "查找城市内真实的团建或休闲目的地，返回地点名称、地址、POI ID 与经纬度。",
            "system_prompt": "你是目的地调研专员。必须先调用 search_destinations 获取真实 POI；可搜索不同关键词，最终至多推荐 3 个候选地点。输出名称、地址、location 坐标和选择依据。门票、营业时间、团队接待能力均标记为待核验；查询失败或没有结果时说明失败，不得编造。",
            "tools": [search_destinations],
            "model": model
        },
        {
             "name": "route-agent",
             "description": "在已知出发位置及候选目的地经纬度后，查询真实单程驾车距离与预计耗时。",
             "system_prompt": "你是交通路线专员。先调用 locate_departure 检索准确的出发地点；若结果存在歧义，必须说明并请求确认，不得猜测。只使用上级任务提供的目的地坐标调用 query_driving_route。逐个报告单程公里数与分钟数，不得臆测包车费用、公交路线或实际交通承诺。遇到工具失败必须报告。",
             "tools": [locate_departure, query_driving_route],
             "model": model
        }
        ,
    ]
