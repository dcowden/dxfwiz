from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dxfwiz.schemas import MachineFile
from dxfwiz.schemas.common import Point2D, StrictModel
from dxfwiz.schemas.job import JobFile
from dxfwiz.toolpaths.model import ToolpathPlan


class SimulationBounds(StrictModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class SimulationStock(StrictModel):
    bounds: SimulationBounds
    thickness: float = Field(gt=0)
    top_z: float = 0.0


class SimulationSettings(StrictModel):
    xy_spacing: float | None = Field(default=None, gt=0)
    xy_tool_fraction: float = Field(default=0.5, gt=0, le=1)
    z_spacing: float | None = Field(default=None, gt=0)
    max_grid_cells: int = Field(default=20_000_000, gt=0)
    preview: bool = True
    arc_chord_fraction: float = Field(default=0.5, gt=0, le=2)


class ExpectedCircleRemoval(StrictModel):
    type: Literal["circle"]
    operation_id: str | None = None
    entity: str | None = None
    center_x: float
    center_y: float
    radius: float = Field(gt=0)
    depth: float = Field(gt=0)


class ExpectedRectangleRemoval(StrictModel):
    type: Literal["rectangle"]
    operation_id: str | None = None
    entity: str | None = None
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    depth: float = Field(gt=0)


class ExpectedPolygonRemoval(StrictModel):
    type: Literal["polygon"]
    operation_id: str | None = None
    entity: str | None = None
    points: list[Point2D] = Field(min_length=3)
    depth: float = Field(gt=0)


class ExpectedSweptLineRemoval(StrictModel):
    type: Literal["swept_line"]
    operation_id: str | None = None
    entity: str | None = None
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    start_depth: float = Field(ge=0)
    end_depth: float = Field(ge=0)
    radius: float = Field(gt=0)


ExpectedRemoval = Annotated[
    ExpectedCircleRemoval | ExpectedRectangleRemoval | ExpectedPolygonRemoval | ExpectedSweptLineRemoval,
    Field(discriminator="type"),
]


class DexelSimulationRequest(StrictModel):
    job: JobFile | None = None
    machine: MachineFile
    toolpath_plan: ToolpathPlan
    stock: SimulationStock
    settings: SimulationSettings = Field(default_factory=SimulationSettings)
    expected_removals: list[ExpectedRemoval] = Field(default_factory=list)


class SimulationIssue(StrictModel):
    code: str
    message: str
    operation: str | None = None
    move_index: int | None = None


class SimulationMetrics(StrictModel):
    grid_width: int
    grid_height: int
    xy_spacing: float
    stock_thickness: float
    removed_cells: int
    expected_removed_cells: int
    overcut_cells: int
    undercut_cells: int
    recut_cells: int
    air_cut_moves: int
    rapid_collision_count: int
    unsafe_rapid_count: int
    max_cut_count: int
    excessive_recut_cells: int = 0
    max_actual_depth: float
    max_expected_depth: float


class SimulationSnapshot(StrictModel):
    x_values: list[float]
    y_values: list[float]
    actual_depth: list[list[float]]
    expected_depth: list[list[float]]
    cut_count: list[list[int]]
    last_operation_id: list[list[int]]
    operation_lookup: dict[int, str]


class DexelSimulationResponse(StrictModel):
    errors: list[SimulationIssue] = Field(default_factory=list)
    warnings: list[SimulationIssue] = Field(default_factory=list)
    metrics: SimulationMetrics
    snapshot: SimulationSnapshot | None = None


ToolEndShape = Literal["flat", "ball", "o-flute"]


class ToolProfile(StrictModel):
    diameter: float = Field(gt=0)
    end_type: ToolEndShape = "flat"
