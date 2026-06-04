from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Units(StrictModel):
    length: Literal["in", "mm"]
    speed: Literal["in/min", "mm/min"] | None = None


class Point2D(StrictModel):
    x: float
    y: float


class Bounds2D(StrictModel):
    min: Point2D
    max: Point2D


class AxisRange(StrictModel):
    min: float
    max: float

