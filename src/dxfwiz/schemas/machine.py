from typing import Literal

from pydantic import Field

from dxfwiz.schemas.common import AxisRange, StrictModel, Units


WorkholdingOption = Literal["clamps", "screws", "tape", "vacuum"]
PartHoldingOption = Literal["onionskin", "z_rollers", "z_presser", "tabs"]
OperationSortPriority = Literal["tool", "group", "role", "nest_order"]


class WorkEnvelope(StrictModel):
    x: AxisRange
    y: AxisRange
    z: AxisRange


class CoordinateSystem(StrictModel):
    x_positive: Literal["right", "left"]
    y_positive: Literal["up", "down"]
    origin: Literal["bottom_left", "bottom_right", "top_left", "top_right", "center"]


class Spindle(StrictModel):
    type: str
    max_rpm: int
    min_rpm: int | None = None


class Machine(StrictModel):
    name: str
    type: Literal["router", "laser", "plasma"]
    axes: int = Field(ge=2, le=3)
    max_tools: int = Field(ge=1)
    clear_z: float
    screw_grid: float | None = Field(default=None, gt=0)
    screw_clearance: float | None = Field(default=None, ge=0)
    operation_sort: list[OperationSortPriority] = Field(default_factory=lambda: ["tool", "group", "nest_order"])
    workholding: list[WorkholdingOption]
    part_holding: list[PartHoldingOption]
    work_envelope: WorkEnvelope
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
