from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon

from dxfwiz.schemas.job import PocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import SourcePath, ToolpathPass
from dxfwiz.toolpaths.operations import (
    _contour_feed_moves,
    _depth_passes,
    _orient_cut_points,
    _polyline_feed_moves,
    _ramped_polyline_feed_moves,
    source_path_points,
)


@dataclass(frozen=True)
class _PocketPath:
    points: list[tuple[float, float]]
    closed: bool


def pocket_operation_to_toolpaths(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    source_polygon = _source_polygon(source_path)
    feed = operation.feed_rate or tool.feed_rate
    cutter_radius = tool.diameter / 2
    side_allowance = operation.roughing.side_allowance if operation.finishing.enabled and operation.finishing.side else 0.0
    bottom_allowance = operation.roughing.bottom_allowance if operation.finishing.enabled and operation.finishing.bottom else 0.0
    rough_depth = max(0.0, operation.depth - bottom_allowance)
    finish_area = _machine_area(source_polygon, cutter_radius)
    rough_area = _machine_area(source_polygon, cutter_radius + side_allowance)
    passes: list[ToolpathPass] = []

    if operation.roughing.enabled and rough_depth > 1e-9:
        passes.append(
            _clearing_pass(
                operation=operation,
                source_path=source_path,
                tool=tool,
                area=rough_area,
                kind="pocket_clear",
                pass_id=f"{operation.id}-rough",
                z_bottom=-rough_depth,
                depths=_depth_passes(rough_depth, operation.roughing.depth_per_pass, tool.depth_per_pass),
                feed=feed,
                safe_z=safe_z,
                offset_distance=cutter_radius + side_allowance,
            )
        )

    if operation.finishing.enabled and operation.finishing.bottom:
        passes.append(
            _clearing_pass(
                operation=operation,
                source_path=source_path,
                tool=tool,
                area=finish_area,
                kind="pocket_floor_finish",
                pass_id=f"{operation.id}-floor-finish",
                z_bottom=-operation.depth,
                depths=[operation.depth],
                feed=feed,
                safe_z=safe_z,
                offset_distance=cutter_radius,
            )
        )

    if operation.finishing.enabled and operation.finishing.side:
        passes.append(
            _wall_finish_pass(
                operation=operation,
                source_path=source_path,
                tool=tool,
                area=finish_area,
                z_bottom=-operation.depth,
                feed=feed,
                safe_z=safe_z,
                offset_distance=cutter_radius,
            )
        )

    return passes


def _clearing_pass(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    area,
    kind: str,
    pass_id: str,
    z_bottom: float,
    depths: list[float],
    feed: float,
    safe_z: float,
    offset_distance: float,
) -> ToolpathPass:
    warnings = _area_warnings(operation, area, offset_distance)
    moves: list[dict] = []
    if not warnings:
        fill_paths = _fill_paths(
            area,
            operation.strategy,
            tool.diameter * operation.stepover_percent / 100,
            operation.roughing.milling_direction,
        )
        for depth in depths:
            z = -depth
            moves.extend(
                _level_moves(
                    fill_paths,
                    z,
                    safe_z,
                    feed,
                    area,
                    ramp=operation.lead_in is None or operation.lead_in.type == "ramp",
                )
            )
    return ToolpathPass(
        id=pass_id,
        operation_id=operation.id,
        entity=operation.entity,
        kind=kind,
        tool=operation.tool,
        tool_diameter=tool.diameter,
        feed_rate=feed,
        z_top=0.0,
        z_bottom=z_bottom,
        source_path=source_path.id,
        offset_side="inside",
        offset_distance=offset_distance,
        milling_direction=operation.roughing.milling_direction,
        moves=moves,
        warnings=warnings,
    )


def _wall_finish_pass(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    area,
    z_bottom: float,
    feed: float,
    safe_z: float,
    offset_distance: float,
) -> ToolpathPass:
    warnings = _area_warnings(operation, area, offset_distance)
    moves: list[dict] = []
    if not warnings:
        for path in _area_exteriors(area, operation.finishing.milling_direction):
            moves.extend(_closed_path_moves(path, z_bottom, safe_z, feed))
    return ToolpathPass(
        id=f"{operation.id}-wall-finish",
        operation_id=operation.id,
        entity=operation.entity,
        kind="pocket_wall_finish",
        tool=operation.tool,
        tool_diameter=tool.diameter,
        feed_rate=feed,
        z_top=0.0,
        z_bottom=z_bottom,
        source_path=source_path.id,
        offset_side="inside",
        offset_distance=offset_distance,
        milling_direction=operation.finishing.milling_direction,
        moves=moves,
        warnings=warnings,
    )


def _source_polygon(source_path: SourcePath) -> Polygon:
    polygon = Polygon(source_path_points(source_path))
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon


def _machine_area(source_polygon: Polygon, offset_distance: float):
    area = source_polygon.buffer(-offset_distance, join_style="round")
    if not area.is_valid:
        area = area.buffer(0)
    return area


def _area_warnings(operation: PocketOperation, area, offset_distance: float) -> list[str]:
    if area.is_empty or area.area <= 1e-12:
        return [
            f"{operation.id}: pocket offset {offset_distance:.6f} leaves no machinable area for entity {operation.entity}",
        ]
    return []


def _fill_paths(area, strategy: str, stepover: float, milling_direction: str | None) -> list[_PocketPath]:
    if strategy == "adaptive":
        return _adaptive_fill_paths(area, stepover, milling_direction)
    if strategy == "offset":
        return _offset_fill_paths(area, stepover, milling_direction)
    return _raster_fill_paths(area, stepover)


def _adaptive_fill_paths(area, stepover: float, milling_direction: str | None) -> list[_PocketPath]:
    """Concentric, loop-preserving clearing inspired by Kiri:Moto area pockets.

    This is not full HSM trochoidal clearing. It keeps each offset ring as a
    closed path and orders rings from the inside outward, which avoids the
    stitched pseudo-spiral backtracking that can make occasional pockets slow.
    """
    paths: list[_PocketPath] = []
    for loop in reversed(_offset_loops(area, stepover, milling_direction)):
        paths.append(_PocketPath(loop, closed=True))
    return _order_closed_paths(paths)


def _offset_fill_paths(area, stepover: float, milling_direction: str | None) -> list[_PocketPath]:
    loops = _offset_loops(area, stepover, milling_direction)
    spiral = _spiral_from_loops(list(reversed(loops)))
    return [_PocketPath(spiral, closed=False)] if len(spiral) >= 2 else []


def _offset_loops(area, stepover: float, milling_direction: str | None) -> list[list[tuple[float, float]]]:
    loops: list[list[tuple[float, float]]] = []
    current = area
    while not current.is_empty and current.area > 1e-12:
        loops.extend(_area_exteriors(current, milling_direction))
        current = current.buffer(-stepover, join_style="round")
    return loops


def _raster_fill_paths(area, stepover: float) -> list[_PocketPath]:
    min_x, min_y, max_x, max_y = area.bounds
    paths: list[_PocketPath] = []
    row = 0
    if max_y - min_y <= stepover:
        y_values = [(min_y + max_y) / 2]
    else:
        y_values = []
        y = min_y + stepover / 2
        while y <= max_y - stepover / 2 + 1e-9:
            y_values.append(y)
            y += stepover
    for y in y_values:
        line = LineString([(min_x - stepover, y), (max_x + stepover, y)])
        intersections = _lines(area.intersection(line))
        intersections.sort(key=lambda item: item.coords[0][0])
        if row % 2:
            intersections.reverse()
        for segment in intersections:
            coords = [(float(x), float(y)) for x, y in segment.coords]
            if row % 2:
                coords.reverse()
            if len(coords) >= 2 and segment.length > 1e-9:
                paths.append(_PocketPath(coords, closed=False))
        row += 1
    return paths


def _spiral_from_loops(loops: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    current: tuple[float, float] | None = None
    for loop in loops:
        if len(loop) < 2:
            continue
        oriented = _orient_path(_PocketPath(loop, closed=True), current)
        if not points:
            points.extend(oriented.points)
        else:
            points.append(oriented.points[0])
            points.extend(oriented.points[1:])
        points.append(oriented.points[0])
        current = points[-1]
    return points


def _area_exteriors(area, milling_direction: str | None = None) -> list[list[tuple[float, float]]]:
    paths: list[list[tuple[float, float]]] = []
    for polygon in _polygons(area):
        coords = [(float(x), float(y)) for x, y in polygon.exterior.coords[:-1]]
        if len(coords) >= 2:
            paths.append(_orient_cut_points(coords, "inside", milling_direction))
    return paths


def _polygons(area) -> list[Polygon]:
    if isinstance(area, Polygon):
        return [area]
    if isinstance(area, MultiPolygon):
        return sorted(area.geoms, key=lambda polygon: polygon.area, reverse=True)
    if isinstance(area, GeometryCollection):
        return [geom for geom in area.geoms if isinstance(geom, Polygon)]
    return []


def _lines(geometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [line for geom in geometry.geoms for line in _lines(geom)]
    return []


def _level_moves(
    paths: list[_PocketPath],
    z_bottom: float,
    safe_z: float,
    feed: float,
    area,
    ramp: bool,
) -> list[dict]:
    moves: list[dict] = []
    current: tuple[float, float] | None = None
    for pocket_path in paths:
        oriented = _orient_path(pocket_path, current)
        if len(oriented.points) < 2:
            continue
        start = oriented.points[0]
        if current is None:
            moves.extend(_enter_path_moves(oriented, z_bottom, safe_z, feed, ramp))
            current = _enter_path_endpoint(oriented, ramp)
        elif _can_cut_link(area, current, start):
            moves.append({"type": "line", "x": start[0], "y": start[1], "z": z_bottom, "feed": feed})
            moves.extend(_cut_path_moves(oriented, z_bottom, feed, include_first=False))
            current = oriented.points[0] if oriented.closed else oriented.points[-1]
        else:
            moves.append({"type": "rapid", "z": safe_z})
            moves.extend(_enter_path_moves(oriented, z_bottom, safe_z, feed, ramp))
            current = _enter_path_endpoint(oriented, ramp)
    if moves:
        moves.append({"type": "rapid", "z": safe_z})
    return moves


def _enter_path_moves(
    pocket_path: _PocketPath,
    z_bottom: float,
    safe_z: float,
    feed: float,
    ramp: bool,
) -> list[dict]:
    points = pocket_path.points
    if len(points) < 2:
        return []
    start = points[0]
    moves: list[dict] = [{"type": "rapid", "x": start[0], "y": start[1], "z": safe_z}]
    if ramp:
        moves.append({"type": "line", "z": 0.0, "feed": feed})
        if pocket_path.closed:
            moves.extend(_closed_path_ramp_entry_moves(points, z_bottom, feed))
            moves.extend(_polyline_feed_moves([*points, points[0]], z_bottom, feed, closed=False))
            return moves
        first_cut = points[1]
        moves.append({"type": "line", "x": first_cut[0], "y": first_cut[1], "z": z_bottom, "feed": feed})
        remaining = points[2:]
        if pocket_path.closed:
            moves.extend(_polyline_feed_moves([points[1], *remaining, points[0]], z_bottom, feed, closed=False))
        else:
            moves.extend(_polyline_feed_moves([points[1], *remaining], z_bottom, feed, closed=False))
        if not pocket_path.closed and len(points) == 2:
            moves.extend(
                {"type": "line", "x": x, "y": y, "z": z_bottom, "feed": feed}
                for x, y in reversed(points[:-1])
            )
    else:
        moves.append({"type": "line", "z": z_bottom, "feed": feed})
        moves.extend(_cut_path_moves(pocket_path, z_bottom, feed, include_first=False))
    return moves


def _closed_path_ramp_entry_moves(
    points: list[tuple[float, float]],
    z_bottom: float,
    feed: float,
) -> list[dict]:
    if len(points) < 2:
        return []
    closed_points = [*points, points[0]]
    segment_lengths = [
        _distance(first, second)
        for first, second in zip(closed_points, closed_points[1:], strict=False)
    ]
    total_length = sum(segment_lengths)
    if total_length <= 1e-9:
        return []
    ramp_points = [(closed_points[0][0], closed_points[0][1], 0.0)]
    traveled = 0.0
    for point, segment_length in zip(closed_points[1:], segment_lengths, strict=False):
        traveled += segment_length
        fraction = min(1.0, traveled / total_length)
        ramp_points.append((point[0], point[1], z_bottom * fraction))
    return _ramped_polyline_feed_moves(ramp_points, feed)


def _enter_path_endpoint(pocket_path: _PocketPath, ramp: bool) -> tuple[float, float]:
    if not pocket_path.closed and ramp:
        return pocket_path.points[0]
    return pocket_path.points[0] if pocket_path.closed else pocket_path.points[-1]


def _cut_path_moves(
    pocket_path: _PocketPath,
    z_bottom: float,
    feed: float,
    include_first: bool,
) -> list[dict]:
    if pocket_path.closed:
        start = pocket_path.points[0]
        if include_first:
            return [
                {"type": "line", "x": start[0], "y": start[1], "z": z_bottom, "feed": feed},
                *_polyline_feed_moves(pocket_path.points, z_bottom, feed, closed=True),
            ]
        return _polyline_feed_moves([*pocket_path.points, start], z_bottom, feed, closed=False)
    if include_first:
        start = pocket_path.points[0]
        return [
            {"type": "line", "x": start[0], "y": start[1], "z": z_bottom, "feed": feed},
            *_polyline_feed_moves(pocket_path.points, z_bottom, feed, closed=False),
        ]
    return _polyline_feed_moves(pocket_path.points, z_bottom, feed, closed=False)


def _orient_path(pocket_path: _PocketPath, current: tuple[float, float] | None) -> _PocketPath:
    if current is None or not pocket_path.points:
        return pocket_path
    points = pocket_path.points
    if pocket_path.closed:
        closest_index = min(
            range(len(points)),
            key=lambda index: _distance_sq(current, points[index]),
        )
        return _PocketPath([*points[closest_index:], *points[:closest_index]], closed=True)
    if _distance_sq(current, points[-1]) < _distance_sq(current, points[0]):
        return _PocketPath(list(reversed(points)), closed=False)
    return pocket_path


def _order_closed_paths(paths: list[_PocketPath]) -> list[_PocketPath]:
    ordered: list[_PocketPath] = []
    remaining = paths[:]
    current: tuple[float, float] | None = None
    while remaining:
        if current is None:
            index = 0
        else:
            index = min(
                range(len(remaining)),
                key=lambda candidate_index: min(
                    _distance_sq(current, point) for point in remaining[candidate_index].points
                ),
            )
        pocket_path = remaining.pop(index)
        pocket_path = _orient_path(pocket_path, current)
        ordered.append(pocket_path)
        current = pocket_path.points[0] if pocket_path.points else current
    return ordered


def _can_cut_link(area, start: tuple[float, float], end: tuple[float, float]) -> bool:
    if _distance_sq(start, end) <= 1e-12:
        return True
    return area.buffer(1e-9).covers(LineString([start, end]))


def _distance_sq(first: tuple[float, float], second: tuple[float, float]) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return _distance_sq(first, second) ** 0.5


def _closed_path_moves(
    points: list[tuple[float, float]],
    z_bottom: float,
    safe_z: float,
    feed: float,
) -> list[dict]:
    if len(points) < 2:
        return []
    start = points[0]
    return [
        {"type": "rapid", "x": start[0], "y": start[1], "z": safe_z},
        {"type": "line", "z": z_bottom, "feed": feed},
        *_contour_feed_moves(points, z_bottom, feed),
        {"type": "rapid", "z": safe_z},
    ]
