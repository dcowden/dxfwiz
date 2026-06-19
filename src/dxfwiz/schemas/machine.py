from typing import Literal

from pydantic import Field, model_validator

from dxfwiz.schemas.common import AxisRange, Point2D, StrictModel, Units


WorkholdingOption = Literal["clamps", "screws", "tape", "vacuum"]
PartHoldingOption = Literal["onionskin", "z_rollers", "z_presser", "tabs"]
OperationSortPriority = Literal["tool", "group", "role", "nest_order"]
ToolpathEngine = Literal["cavalier", "legacy"]


class WorkEnvelope(StrictModel):
    x: AxisRange
    y: AxisRange
    z: AxisRange


class AxisMotionLimits(StrictModel):
    x: float = Field(default=500.0, gt=0)
    y: float = Field(default=500.0, gt=0)
    z: float = Field(default=500.0, gt=0)


class MachineKinematics(StrictModel):
    max_velocity: AxisMotionLimits = Field(default_factory=AxisMotionLimits)
    acceleration: AxisMotionLimits = Field(
        default_factory=lambda: AxisMotionLimits(x=1000.0, y=1000.0, z=1000.0)
    )


class CoordinateSystem(StrictModel):
    x_positive: Literal["right", "left"]
    y_positive: Literal["up", "down"]
    origin: Literal["bottom_left", "bottom_right", "top_left", "top_right", "center"]


class Spindle(StrictModel):
    type: str
    max_rpm: int
    min_rpm: int | None = None


class BasicWorkholdingMethod(StrictModel):
    method: Literal["clamps", "tape", "vacuum"]


class ScrewWorkholdingMethod(StrictModel):
    method: Literal["screws"]
    screw_hole_diameter: float = Field(default=0.125, gt=0)
    screw_grid: float | None = Field(default=None, gt=0)
    screw_grid_offset: Point2D = Field(default_factory=lambda: Point2D(x=0.0, y=0.0))
    screw_clearance: float | None = Field(default=None, ge=0)
    allow_oversized_holes_to_prevent_toolchange: bool = True


WorkholdingMethod = BasicWorkholdingMethod | ScrewWorkholdingMethod


class WorkholdingConfig(StrictModel):
    supported_methods: list[WorkholdingMethod]

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_list(cls, data):
        if isinstance(data, list):
            return {
                "supported_methods": [
                    {"method": item} if isinstance(item, str) else item
                    for item in data
                ]
            }
        return data

    @property
    def method_names(self) -> list[WorkholdingOption]:
        return [method.method for method in self.supported_methods]


class Machine(StrictModel):
    name: str
    type: Literal["router", "laser", "plasma"]
    axes: int = Field(ge=2, le=3)
    max_tools: int = Field(ge=1)
    separate_nc_file_per_tool: bool = False
    clear_z: float
    screw_grid: float | None = Field(default=None, gt=0)
    screw_grid_offset: Point2D = Field(default_factory=lambda: Point2D(x=0.0, y=0.0))
    screw_clearance: float | None = Field(default=None, ge=0)
    maximum_plug_size: float = Field(default=0.25, gt=0)
    operation_sort: list[OperationSortPriority] = Field(default_factory=lambda: ["tool", "group", "nest_order"])
    toolpath_engine: ToolpathEngine = "cavalier"
    workholding: WorkholdingConfig
    part_holding: list[PartHoldingOption]
    work_envelope: WorkEnvelope
    kinematics: MachineKinematics = Field(default_factory=MachineKinematics)
    coordinate_system: CoordinateSystem
    spindle: Spindle


class Tool(StrictModel):
    id: str
    description: str
    end_type: Literal["flat", "ball", "o-flute"]
    flute_spiral: Literal["straight", "downcut", "upcut", "compression"]
    diameter: float = Field(gt=0)
    flutes: int = Field(gt=0)
    speed: int = Field(gt=0)
    feed_rate: float = Field(gt=0)
    plunge_rate: float = Field(gt=0)
    depth_per_pass: float | None = Field(default=None, gt=0)
    step_over: float | None = Field(default=None, gt=0)
    flute_length: float | None = Field(default=None, gt=0)
    total_length: float | None = Field(default=None, gt=0)


class MachineFile(StrictModel):
    schema_version: str
    units: Units
    machine: Machine
    tools: list[Tool]
