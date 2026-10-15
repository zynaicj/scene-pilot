"""摄影选景需求数据模型；不把未知费用、日期和场地条件设为默认已确认。"""
from datetime import date
from pydantic import BaseModel, Field, field_validator


class PhotographyBrief(BaseModel):
    """摄影师人像约拍需求，字段可逐步补齐，城市与拍摄风格为核心条件。"""

    city: str = Field(min_length=1, max_length=60, description="拍摄城市，例如杭州")
    photography_style: str = Field(min_length=1, max_length=100, description="摄影风格，例如日系清新")
    departure: str | None = Field(default=None, max_length=120, description="实际出发地点")
    shooting_date: date | None = Field(default=None, description="明确的 YYYY-MM-DD 日期；不明确时留空")
    clothing: str | None = Field(default=None, max_length=300, description="服饰颜色、款式和材质")
    makeup: str | None = Field(default=None, max_length=300, description="模特妆容、发型及饰品")
    desired_scenes: list[str] = Field(default_factory=list, description="树林、草地、街景等目标环境")
    lighting: str | None = Field(default=None, max_length=120, description="柔光、逆光、蓝调等")
    reference_description: str | None = Field(default=None, max_length=500, description="文字描述的参考照片，本阶段不做图像识别")
    transport_mode: str | None = Field(default=None, max_length=40, description="步行、驾车或公共交通")
    max_travel_minutes: int | None = Field(default=None, ge=0, le=1440, description="单程可接受分钟数")
    max_budget_cny: float | None = Field(default=None, ge=0, description="此次拍摄总预算上限，单位元")
    equipment: list[str] = Field(default_factory=list, description="镜头、灯光等设备")
    crew_size: int | None = Field(default=None, ge=1, le=100, description="拍摄团队总人数")
    venue_requirements: list[str] = Field(default_factory=list, description="换装便利、拍摄许可、隐私等条件")

    @field_validator("city", "photography_style")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("城市和摄影风格不能只有空格")
        return value

    @field_validator("max_budget_cny")
    @classmethod
    def finite_budget(cls, value: float | None) -> float | None:
        import math
        if value is not None and not math.isfinite(value):
            raise ValueError("预算必须是有限数字")
        return value


def make_verification_queries(place_name: str, style: str) -> list[str]:
    """提供社交媒体搜索词，不调用或抓取任何社交平台。"""
    place = place_name.strip()
    if not place:
        raise ValueError("必须提供真实地点名称")
    return [
        f"{place} {style.strip()} 人像写真 实拍",
        f"{place} 近期实拍 摄影机位",
        f"{place} 商业拍摄许可 换装",
    ]
