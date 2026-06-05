from typing import Literal

from pydantic import Field

from dxfwiz.schemas.common import StrictModel, Units
from dxfwiz.schemas.machine import PartHoldingOption, WorkholdingOption


class PlannerInfo(StrictModel):
    name: str
    description: str | None = None


class StockDefaults(StrictModel):
    material: str
    thickness: float = Field(gt=0)
    z_zero: Literal["stock_top", "spoilboard_top"]


class TabSettings(StrictModel):
    enabled: bool
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    spacing: float | None = Field(default=None, gt=0)


class Defaults(StrictModel):
    max_tools: int | None = Field(default=None, ge=1)
    coordinate_system: str | None = None
    stock: StockDefaults | None = None
    origin_location: Literal["bottom_left", "bottom_right", "top_left", "top_right", "center"]
    workholding: list[WorkholdingOption] = Field(default_factory=list)
    part_holding: list[PartHoldingOption] = Field(default_factory=list)
    default_tool: str | None = None
    milling_direction: Literal["climb", "conventional"]
    finishing_allowance: float = Field(ge=0)
    tab_settings: TabSettings


class OperationAdvice(StrictModel):
    workholding: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    geometry: list[str] = Field(default_factory=list)
    strategies: list[str] = Field(default_factory=list)


class PlannerFile(StrictModel):
    schema_version: str
    units: Units
    planner: PlannerInfo
    defaults: Defaults
    operation_advice: OperationAdvice
