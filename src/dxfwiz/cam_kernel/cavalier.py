from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from dxfwiz.schemas.common import Point2D
from dxfwiz.toolpaths.model import SourceArcSegment, SourceLineSegment, SourcePath

try:
    import dxfwiz_cavc as _cavc
except ImportError:  # pragma: no cover - exercised on systems without the native module.
    _cavc = None


@dataclass(frozen=True)
class BulgeVertex:
    x: float
    y: float
    bulge: float = 0.0


class CavalierUnavailable(RuntimeError):
    pass


def is_available() -> bool:
    return _cavc is not None


def native():
    if _cavc is None:
        raise CavalierUnavailable("dxfwiz_cavc native module is not installed")
    return _cavc


def offset_source_path(
    source_path: SourcePath,
    offset_side: str,
    distance: float,
) -> list[SourcePath]:
    if _cavc is None:
        raise CavalierUnavailable("dxfwiz_cavc native module is not installed")
    vertices = source_path_to_bulge_vertices(source_path)
    orientation_sign = 1 if _cavc.polyline_area([vertex_tuple(vertex) for vertex in vertices], source_path.closed) >= 0 else -1
    signed_distance = distance * orientation_sign if offset_side == "inside" else -distance * orientation_sign
    offset_loops = _cavc.offset_polyline(
        [vertex_tuple(vertex) for vertex in vertices],
        signed_distance,
        source_path.closed,
    )
    return [
        bulge_vertices_to_source_path(
            source_path.id,
            source_path.entity,
            [BulgeVertex(x, y, bulge) for x, y, bulge in vertices],
            closed=True,
            suffix=f"cavc-{index}",
        )
        for index, vertices in enumerate(offset_loops, start=1)
        if len(vertices) >= 2
    ]


def source_path_to_bulge_vertices(source_path: SourcePath) -> list[BulgeVertex]:
    vertices: list[BulgeVertex] = []
    for segment in source_path.segments:
        start = _xy(segment.start)
        if isinstance(segment, SourceLineSegment):
            _append_vertex(vertices, BulgeVertex(start[0], start[1], 0.0))
        else:
            sweep = _source_arc_sweep(segment)
            if abs(sweep) > math.pi * 2 - 1e-9 and _same_point(segment.start, segment.end):
                midpoint = _arc_midpoint(segment)
                half_bulge = math.tan((sweep / 2) / 4)
                _append_vertex(vertices, BulgeVertex(start[0], start[1], half_bulge))
                _append_vertex(vertices, BulgeVertex(midpoint[0], midpoint[1], half_bulge))
                continue
            if abs(sweep) > math.pi + 1e-9:
                raise ValueError("Cavalier bulge arcs must be half-circles or smaller")
            _append_vertex(vertices, BulgeVertex(start[0], start[1], math.tan(sweep / 4)))
    if source_path.closed and len(vertices) > 1 and _same_xy(vertices[0], vertices[-1]):
        vertices.pop()
    return vertices


def bulge_vertices_to_source_path(
    source_id: str,
    entity: str,
    vertices: Iterable[BulgeVertex],
    closed: bool = True,
    suffix: str = "cavc",
) -> SourcePath:
    vertex_list = list(vertices)
    segments = []
    if len(vertex_list) < 2:
        return SourcePath(id=f"{source_id}-{suffix}", entity=entity, closed=closed, segments=[])
    pairs = zip(vertex_list, [*vertex_list[1:], vertex_list[0]] if closed else vertex_list[1:], strict=False)
    for start, end in pairs:
        start_point = Point2D(x=start.x, y=start.y)
        end_point = Point2D(x=end.x, y=end.y)
        if abs(start.bulge) <= 1e-12:
            segments.append(SourceLineSegment(type="line", start=start_point, end=end_point))
            continue
        center, radius, direction = _bulge_arc_geometry(start, end)
        segments.append(
            SourceArcSegment(
                type="arc",
                start=start_point,
                end=end_point,
                center=Point2D(x=center[0], y=center[1]),
                radius=radius,
                direction=direction,
            )
        )
    return SourcePath(id=f"{source_id}-{suffix}", entity=entity, closed=closed, segments=segments)


def vertex_tuple(vertex: BulgeVertex) -> tuple[float, float, float]:
    return vertex.x, vertex.y, vertex.bulge


def _source_arc_sweep(segment: SourceArcSegment) -> float:
    start = _xy(segment.start)
    end = _xy(segment.end)
    center = _xy(segment.center)
    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = end_angle - start_angle
    if segment.direction == "ccw" and sweep <= 0:
        sweep += 2 * math.pi
    if segment.direction == "cw" and sweep >= 0:
        sweep -= 2 * math.pi
    return sweep


def _arc_midpoint(segment: SourceArcSegment) -> tuple[float, float]:
    start = _xy(segment.start)
    center = _xy(segment.center)
    return center[0] - (start[0] - center[0]), center[1] - (start[1] - center[1])


def _bulge_arc_geometry(
    start: BulgeVertex,
    end: BulgeVertex,
) -> tuple[tuple[float, float], float, str]:
    chord_x = end.x - start.x
    chord_y = end.y - start.y
    chord = math.hypot(chord_x, chord_y)
    if chord <= 1e-12:
        raise ValueError("arc segment has zero-length chord")
    sweep = 4 * math.atan(start.bulge)
    radius = chord / (2 * abs(math.sin(sweep / 2)))
    midpoint = ((start.x + end.x) / 2, (start.y + end.y) / 2)
    sagitta = start.bulge * chord / 2
    center_distance = radius - abs(sagitta)
    normal = (-chord_y / chord, chord_x / chord)
    sign = 1 if start.bulge > 0 else -1
    center = (
        midpoint[0] + normal[0] * center_distance * sign,
        midpoint[1] + normal[1] * center_distance * sign,
    )
    return center, radius, "ccw" if start.bulge > 0 else "cw"


def _append_vertex(vertices: list[BulgeVertex], vertex: BulgeVertex) -> None:
    if vertices and _same_xy(vertices[-1], vertex):
        vertices[-1] = vertex
    else:
        vertices.append(vertex)


def _same_xy(first: BulgeVertex, second: BulgeVertex) -> bool:
    return abs(first.x - second.x) <= 1e-9 and abs(first.y - second.y) <= 1e-9


def _same_point(first: Point2D, second: Point2D) -> bool:
    return abs(first.x - second.x) <= 1e-9 and abs(first.y - second.y) <= 1e-9


def _xy(point: Point2D) -> tuple[float, float]:
    return float(point.x), float(point.y)
