"""保留 V0.1 中的确定性预算工具。"""
from decimal import Decimal, InvalidOperation
from langchain_core.tools import tool


@tool
def calculate_group_budget(member_count: int, budget_per_person: float) -> dict:
    """根据人数和人均预算计算可用总预算（人民币元）；不代表真实行程报价。"""
    try:
        amount = Decimal(str(budget_per_person))
        if member_count <= 0 or not amount.is_finite() or amount < 0:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        return {"status": "error", "message": "人数必须大于零，人均预算不得为负数"}
    return {"status": "success", "member_count": member_count, "budget_per_person": str(amount), "total_budget": str(amount * member_count), "currency": "CNY"}
