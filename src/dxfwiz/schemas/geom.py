from typing import Literal

from pydantic import Field

from dxfwiz.schemas.common import Bounds2D, Point2D, StrictModel


class GeometryUnits(StrictModel):
    length: Literal["in", "mm"]
    source: Literal["explicit_dxf", "guessed"]
    confidence: float = Field(ge=0, le=1)
    coordinate_scale: float = Field(gt=0)
    evidence: list[str] = Field(default_factory=list)


class SourceFiles(StrictModel):
    original_file: str
    cleaned_file: str
    format: Literal["dxf", "svg"]


class Summary(StrictModel):
    entity_count: int = Field(ge=0)
    closed_count: int = Field(ge=0)
    open_count: int = Field(ge=0)
    ignored_count: int = Field(default=0, ge=0)
    generated_count: int = Field(default=0, ge=0)
    bounding_box: Bounds2D


class EntityRef(StrictModel):
    kind: Literal["dxf_handle", "dxfwiz_id"]
    value: str


class GeometryEntity(StrictModel):
    id: str
    type: Literal["closed_loop", "open_path"]
    shape: str
    source_refs: list[EntityRef]
    bounding_box: Bounds2D | None = None
    center: Point2D | None = None
    diameter: float | None = Field(default=None, gt=0)
    area: float | None = Field(default=None, ge=0)
    perimeter: float | None = Field(default=None, ge=0)


class GeneratedGeometryEntity(StrictModel):
    id: str
    role: Literal["screw_hole", "tab", "clamp"]
    shape: Literal["circle", "rectangle"]
    center: Point2D | None = None
    diameter: float | None = Field(default=None, gt=0)
    lower_left: Point2D | None = None
    upper_right: Point2D | None = None
    source: Literal["planner"] = "planner"


class ContainmentNode(StrictModel):
    entity: str
    role: Literal[
        "frame",
        "part",
        "cutout",
        "island",
        "outer_boundary",
        "hole_candidate",
        "uncontained",
        "ignored",
    ]
    children: list["ContainmentNode"] = Field(default_factory=list)


class GeometryFile(StrictModel):
    schema_version: str
    units: GeometryUnits
    source: SourceFiles
    summary: Summary
    entity_map: list[ContainmentNode]
    generated_entities: list[GeneratedGeometryEntity] = Field(default_factory=list)
    entities: list[GeometryEntity]
