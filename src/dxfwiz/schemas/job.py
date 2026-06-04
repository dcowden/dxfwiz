from typing import Literal

from pydantic import Field

from dxfwiz.schemas.common import StrictModel, Units


class JobInfo(StrictModel):
    name: str
    description: str | None = None
    created: str | None = None
    geometry_file: str
    machine: str
    post: str
    planner: str


class Stock(StrictModel):
    material: str
    thickness: float = Field(gt=0)
    z_zero: Literal["stock_top", "spoilboard_top"]
    origin_location: Literal["bottom_left", "bottom_right", "top_left", "top_right", "center"]


class Tabs(StrictModel):
    enabled: bool
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    count: int | None = Field(default=None, gt=0)
    spacing: float | None = Field(default=None, gt=0)


class LeadIn(StrictModel):
    type: Literal["arc", "line"]
    radius: float | None = Field(default=None, gt=0)
    length: float | None = Field(default=None, gt=0)


class FinishingPass(StrictModel):
    enabled: bool
    allowance: float = Field(ge=0)


class Operation(StrictModel):
    id: str
    type: Literal["contour", "pocket", "drill", "helical_drill", "trace"]
    description: str | None = None
    entity: str
    tool: str
    depth: float = Field(gt=0)
    offset: Literal["outside", "inside", "none"] | None = None
    milling_direction: Literal["climb", "conventional"] | None = None
    finishing_allowance: float | None = Field(default=None, ge=0)
    tabs: Tabs | None = None
    lead_in: LeadIn | None = None
    peck_depth: float | None = Field(default=None, gt=0)
    retract_amount: float | None = Field(default=None, gt=0)
    dwell_time: float | None = Field(default=None, ge=0)
    stepover_percent: float | None = Field(default=None, gt=0, le=100)
    strategy: str | None = None
    finishing_pass: FinishingPass | None = None
    speed: int | None = Field(default=None, gt=0)
    feed_rate: float | None = Field(default=None, gt=0)
    plunge_rate: float | None = Field(default=None, gt=0)
    depth_per_pass: float | None = Field(default=None, gt=0)


class JobFile(StrictModel):
    schema_version: str
    units: Units
    job: JobInfo
    stock: Stock
    coordinate_system: str
    operations: list[Operation]
