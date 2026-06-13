from typing import Annotated, Literal

from pydantic import AliasChoices, Field, model_validator

from dxfwiz.schemas.common import Point2D, StrictModel, Units


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
    count: int | None = Field(default=None, ge=0)
    spacing: float | None = Field(default=None, gt=0)
    locations: list["TabLocation"] = Field(default_factory=list)


class TabLocation(StrictModel):
    center: Point2D
    lower_left: Point2D
    upper_right: Point2D
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    angle_deg: float | None = None


class LeadIn(StrictModel):
    type: Literal["arc", "line", "ramp"]
    radius: float | None = Field(default=None, gt=0)
    length: float | None = Field(default=None, gt=0)
    ramp_angle_deg: float | None = Field(default=None, gt=0)


class Roughing(StrictModel):
    enabled: bool = True
    depth_per_pass: float = Field(gt=0)
    side_allowance: float = Field(default=0.0, ge=0)
    bottom_allowance: float = Field(default=0.0, ge=0)
    milling_direction: Literal["climb", "conventional"]


class Finishing(StrictModel):
    enabled: bool = False
    side: bool = True
    bottom: bool = False
    passes: int = Field(default=1, ge=1)
    milling_direction: Literal["climb", "conventional"] | None = None


class BaseOperation(StrictModel):
    id: str
    description: str | None = None
    entity: str
    tool: str
    depth: float = Field(gt=0)
    spindle_speed: int | None = Field(default=None, gt=0, validation_alias=AliasChoices("spindle_speed", "speed"))
    feed_rate: float | None = Field(default=None, gt=0)
    plunge_rate: float | None = Field(default=None, gt=0)


class ContourOperation(BaseOperation):
    type: Literal["contour"]
    offset: Literal["outside", "inside", "on"]
    extra_depth: float = Field(default=0.0, ge=0)
    ramping: bool = False
    roughing: Roughing
    finishing: Finishing = Field(default_factory=Finishing)
    tabs: Tabs | None = None
    lead_in: LeadIn | None = None


class PocketOperation(BaseOperation):
    type: Literal["pocket"]
    strategy: Literal["offset", "raster", "adaptive"] = "offset"
    stepover_percent: float = Field(gt=0, le=100)
    roughing: Roughing
    finishing: Finishing = Field(default_factory=lambda: Finishing(enabled=True, side=True, bottom=True))
    lead_in: LeadIn | None = None


class DrillOperation(BaseOperation):
    type: Literal["drill"]
    peck_depth: float = Field(gt=0)
    retract_amount: float = Field(gt=0)
    dwell_time: float | None = Field(default=None, ge=0)


class HelicalContourOperation(BaseOperation):
    type: Literal["helical_contour"]
    hole_diameter: float | None = Field(default=None, gt=0)
    pitch: float = Field(gt=0)
    milling_direction: Literal["climb", "conventional"]
    skip_roughing_when_finish_fits: bool = True
    finishing: Finishing = Field(default_factory=lambda: Finishing(enabled=True, side=True, bottom=False))
    lead_in: LeadIn | None = None


class HelicalPocketOperation(BaseOperation):
    type: Literal["helical_pocket"]
    hole_diameter: float = Field(gt=0)
    pitch: float = Field(gt=0)
    stepover_percent: float = Field(gt=0, le=100)
    prefer_arcs: bool = True
    roughing: Roughing
    finishing: Finishing = Field(default_factory=lambda: Finishing(enabled=True, side=True, bottom=True))
    milling_direction: Literal["climb", "conventional"]
    lead_in: LeadIn | None = None


class TraceOperation(BaseOperation):
    type: Literal["trace"]
    roughing: Roughing
    lead_in: LeadIn | None = None


class MoveOperation(StrictModel):
    id: str
    type: Literal["move"]
    description: str | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    is_rapid: bool = True
    feed_rate: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def at_least_one_axis(self) -> "MoveOperation":
        if self.x is None and self.y is None and self.z is None:
            raise ValueError("move operation requires at least one of x, y, or z")
        return self


Operation = Annotated[
    ContourOperation | PocketOperation | DrillOperation | HelicalContourOperation | HelicalPocketOperation | TraceOperation | MoveOperation,
    Field(discriminator="type"),
]


class ToolUse(StrictModel):
    tool: str
    diameter: float = Field(gt=0)


class OperationGroup(StrictModel):
    name: str
    operations: list[str]


class GeneratedEntity(StrictModel):
    id: str
    role: Literal["screw_hole", "tab", "clamp"]
    shape: Literal["circle", "rectangle"]
    center: Point2D | None = None
    diameter: float | None = Field(default=None, gt=0)
    lower_left: Point2D | None = None
    upper_right: Point2D | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    angle_deg: float | None = None


class JobFile(StrictModel):
    schema_version: str
    units: Units
    job: JobInfo
    stock: Stock
    coordinate_system: str
    tools: list[ToolUse] = Field(default_factory=list)
    generated_entities: list[GeneratedEntity] = Field(default_factory=list)
    operation_groups: list[OperationGroup] = Field(default_factory=list)
    operations: list[Operation]
