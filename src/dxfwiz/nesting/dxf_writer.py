from __future__ import annotations

from math import radians
from pathlib import Path

import ezdxf
from shapely import affinity

from dxfwiz.nesting.engine import NestingResult, PartShape


def write_nested_dxf(
    result: NestingResult,
    parts: list[PartShape],
    output_path: str | Path,
    *,
    border: float = 1.0,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    min_x, min_y, max_x, max_y = _layout_bounds(result)
    width = (max_x - min_x) + border * 2
    height = (max_y - min_y) + border * 2

    doc = ezdxf.new("R2000")
    doc.units = 1
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (width, 0), (width, height), (0, height)],
        close=True,
        dxfattribs={"layer": "DXFWIZ_FRAME"},
    )

    for placement in result.placements:
        part = parts[placement.part_index]
        source_doc = ezdxf.readfile(part.path)
        rotation_min_x, rotation_min_y = _rotated_min(part, placement.rotation)
        dx = -rotation_min_x + placement.x - min_x + border
        dy = -rotation_min_y + placement.y - min_y + border

        for entity in source_doc.modelspace():
            copied = entity.copy()
            if placement.rotation:
                copied.rotate_z(radians(placement.rotation))
            copied.translate(dx, dy, 0.0)
            copied.dxf.discard("handle")
            msp.add_entity(copied)

    doc.saveas(output_path)
    return output_path


def _layout_bounds(result: NestingResult) -> tuple[float, float, float, float]:
    if not result.placements:
        return 0.0, 0.0, 0.0, 0.0
    bounds = [placement.geometry.bounds for placement in result.placements]
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )


def _rotated_min(part: PartShape, rotation: int) -> tuple[float, float]:
    geometry = affinity.rotate(part.geometry, rotation, origin=(0, 0), use_radians=False)
    min_x, min_y, _max_x, _max_y = geometry.bounds
    return min_x, min_y
