from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from dxfwiz.schemas.common import Point2D, StrictModel
from dxfwiz.schemas.geom import EntityRef


Direction = Literal["cw", "ccw"]
MillingDirection = Literal["climb", "conventional"]


class Point3D(StrictModel):
    x: float
    y: float
    z: float


class SourceLineSegment(StrictModel):
    type: Literal["line"]
    start: Point2D
    end: Point2D


class SourceArcSegment(StrictModel):
    type: Literal["arc"]
    start: Point2D
    end: Point2D
    center: Point2D
    radius: float = Field(gt=0)
    direction: Direction


class SourcePath(StrictModel):
    id: str
    entity: str
    closed: bool
    segments: list[Annotated[SourceLineSegment | SourceArcSegment, Field(discriminator="type")]]
    source_refs: list[EntityRef] = Field(default_factory=list)


class RapidMove(StrictModel):
    type: Literal["rapid"]
    x: float | None = None
    y: float | None = None
    z: float | None = None


class LineMove(StrictModel):
    type: Literal["line"]
    x: float | None = None
    y: float | None = None
    z: float | None = None
    feed: float | None = Field(default=None, gt=0)


class ArcMove(StrictModel):
    type: Literal["arc"]
    direction: Direction
    x: float
    y: float
    z: float | None = None
    i: float
    j: float
    feed: float | None = Field(default=None, gt=0)


class DwellMove(StrictModel):
    type: Literal["dwell"]
    seconds: float = Field(ge=0)


class ToolChangeMove(StrictModel):
    type: Literal["tool_change"]
    tool: str


class SpindleMove(StrictModel):
    type: Literal["spindle"]
    state: Literal["on", "off"]
    rpm: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def rpm_required_when_on(self) -> "SpindleMove":
        if self.state == "on" and self.rpm is None:
            raise ValueError("rpm is required when spindle state is on")
        return self


ToolpathMove = Annotated[
    RapidMove | LineMove | ArcMove | DwellMove | ToolChangeMove | SpindleMove,
    Field(discriminator="type"),
]


class ToolpathPass(StrictModel):
    id: str
    operation_id: str
    entity: str | None = None
    kind: Literal[
        "rough_contour",
        "finish_contour",
        "pocket_clear",
        "pocket_floor_finish",
        "pocket_wall_finish",
        "peck_drill",
        "helical_drill",
        "trace",
        "move",
    ]
    tool: str | None = None
    tool_diameter: float | None = Field(default=None, gt=0)
    z_top: float = 0.0
    z_bottom: float
    source_path: str | None = None
    offset_side: Literal["outside", "inside", "on"] | None = None
    offset_distance: float | None = Field(default=None, ge=0)
    milling_direction: MillingDirection | None = None
    moves: list[ToolpathMove] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ToolpathPlan(StrictModel):
    schema_version: str = "1.0"
    units: Literal["in", "mm"]
    coordinate_system: str
    source_paths: list[SourcePath] = Field(default_factory=list)
    passes: list[ToolpathPass] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
