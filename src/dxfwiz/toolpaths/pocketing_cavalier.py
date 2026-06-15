from __future__ import annotations

import math

from shapely.geometry import LineString, MultiLineString, Polygon

from dxfwiz.cam_kernel.cavalier import CavalierUnavailable, offset_source_path
from dxfwiz.schemas.job import ContourOperation, HelicalContourOperation, HelicalPocketOperation, PocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.drilling import _finish_circle_moves, _helical_pocket_moves, _helix_moves
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove, SourceArcSegment, SourceLineSegment, SourcePath, ToolpathPass
from dxfwiz.toolpaths.operations import (
    _add_extra_depth_to_final_pass,
    _closed_line_moves,
    _depth_passes,
    _layered_line_moves,
    _spiral_line_moves,
    _unmachinable_contour_pass,
    source_path_points,
)


def pocket_operation_to_cavalier_toolpaths(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    """Experimental Cavalier-backed pocket offsets.

    This intentionally lives beside the Shapely pocketing implementation while
    the native kernel proves itself on SVG fixtures.
    """
    cutter_radius = tool.diameter / 2
    feed = operation.feed_rate or tool.feed_rate
    side_allowance = operation.roughing.side_allowance if operation.finishing.enabled and operation.finishing.side else 0.0
    bottom_allowance = operation.roughing.bottom_allowance if operation.finishing.enabled and operation.finishing.bottom else 0.0
    rough_depth = max(0.0, operation.depth - bottom_allowance)
    passes: list[ToolpathPass] = []

    if operation.roughing.enabled and rough_depth > 1e-9:
        passes.append(
            _offset_clearing_pass(
                operation,
                source_path,
                tool,
                "pocket_clear",
                f"{operation.id}-cavc-rough",
                cutter_radius + side_allowance,
                _depth_passes(rough_depth, operation.roughing.depth_per_pass, tool.depth_per_pass),
                feed,
                safe_z,
            )
        )

    if operation.finishing.enabled and operation.finishing.bottom:
        passes.append(
            _offset_clearing_pass(
                operation,
                source_path,
                tool,
                "pocket_floor_finish",
                f"{operation.id}-cavc-floor-finish",
                cutter_radius,
                [operation.depth],
                feed,
                safe_z,
            )
        )

    if operation.finishing.enabled and operation.finishing.side:
        wall_loops = offset_source_path(source_path, "inside", cutter_radius)
        moves = []
        for loop in wall_loops:
            moves.extend(_closed_source_path_moves(loop, -operation.depth, safe_z, feed))
        passes.append(
            ToolpathPass(
                id=f"{operation.id}-cavc-wall-finish",
                operation_id=operation.id,
                entity=operation.entity,
                kind="pocket_wall_finish",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=-operation.depth,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=cutter_radius,
                milling_direction=operation.finishing.milling_direction,
                moves=moves,
            )
        )
    return passes


def contour_operation_to_cavalier_toolpaths(
    operation: ContourOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    """Experimental Cavalier-backed contour offsets that preserve arc segments."""
    passes: list[ToolpathPass] = []
    total_depth = operation.depth + operation.extra_depth
    feed = operation.feed_rate or tool.feed_rate

    if operation.roughing.enabled:
        rough_offset = _contour_offset_distance(operation.offset, tool.diameter, operation.roughing.side_allowance)
        rough_paths = _offset_paths(source_path, operation.offset, rough_offset)
        depths = _depth_passes(operation.depth, operation.roughing.depth_per_pass, tool.depth_per_pass)
        depths = _add_extra_depth_to_final_pass(depths, operation.extra_depth, tool.depth_per_pass)
        if rough_paths:
            moves = []
            for path in rough_paths:
                if _has_active_tabs(operation):
                    points = source_path_points(path)
                    if operation.ramping:
                        moves.extend(
                            _spiral_line_moves(
                                points,
                                depths,
                                safe_z,
                                feed,
                                bottom_cleanup=not (operation.finishing.enabled and operation.finishing.side),
                                operation=operation,
                                tool=tool,
                            )
                        )
                    else:
                        moves.extend(_layered_line_moves(points, depths, safe_z, feed, operation, tool))
                elif operation.ramping:
                    moves.extend(
                        _ramped_source_path_moves(
                            path,
                            depths,
                            safe_z,
                            feed,
                            bottom_cleanup=not (operation.finishing.enabled and operation.finishing.side),
                        )
                    )
                else:
                    for depth in depths:
                        moves.extend(_closed_source_path_moves(path, -depth, safe_z, feed))
            passes.append(
                ToolpathPass(
                    id=f"{operation.id}-cavc-rough",
                    operation_id=operation.id,
                    entity=operation.entity,
                    kind="rough_contour",
                    tool=operation.tool,
                    tool_diameter=tool.diameter,
                    feed_rate=feed,
                    z_top=0.0,
                    z_bottom=-depths[-1],
                    source_path=source_path.id,
                    offset_side=operation.offset,
                    offset_distance=abs(rough_offset),
                    milling_direction=operation.roughing.milling_direction,
                    moves=moves,
                )
            )
        else:
            passes.append(
                _unmachinable_contour_pass(
                    operation,
                    source_path,
                    tool,
                    "rough_contour",
                    f"{operation.id}-cavc-rough",
                    -depths[-1],
                    abs(rough_offset),
                )
            )

    if operation.finishing.enabled and operation.finishing.side:
        finish_offset = _contour_offset_distance(operation.offset, tool.diameter, 0.0)
        finish_paths = _offset_paths(source_path, operation.offset, finish_offset)
        if finish_paths:
            for index in range(1, operation.finishing.passes + 1):
                moves = []
                for path in finish_paths:
                    if _has_active_tabs(operation):
                        moves.extend(
                            _closed_line_moves(source_path_points(path), -total_depth, safe_z, feed, operation, tool)
                        )
                    else:
                        moves.extend(_closed_source_path_moves(path, -total_depth, safe_z, feed))
                passes.append(
                    ToolpathPass(
                        id=f"{operation.id}-cavc-finish-{index}",
                        operation_id=operation.id,
                        entity=operation.entity,
                        kind="finish_contour",
                        tool=operation.tool,
                        tool_diameter=tool.diameter,
                        feed_rate=feed,
                        z_top=0.0,
                        z_bottom=-total_depth,
                        source_path=source_path.id,
                        offset_side=operation.offset,
                        offset_distance=abs(finish_offset),
                        milling_direction=operation.finishing.milling_direction,
                        moves=moves,
                    )
                )
        else:
            passes.extend(
                _unmachinable_contour_pass(
                    operation,
                    source_path,
                    tool,
                    "finish_contour",
                    f"{operation.id}-cavc-finish-{index}",
                    -total_depth,
                    abs(finish_offset),
                )
                for index in range(1, operation.finishing.passes + 1)
            )
    return passes


def helical_contour_operation_to_cavalier_toolpaths(
    operation: HelicalContourOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
    finishing_allowance: float = 0.01,
) -> list[ToolpathPass]:
    """Experimental helical contour from the actual circle path, offset by Cavalier."""
    target = -operation.depth
    feed = operation.feed_rate or tool.feed_rate
    cutter_radius = tool.diameter / 2
    direction = "ccw" if operation.milling_direction == "climb" else "cw"
    _source_center, source_radius = _circle_geometry_from_source_path(source_path)
    finish_radius = source_radius - cutter_radius
    if finish_radius <= 0:
        return [
            ToolpathPass(
                id=f"{operation.id}-cavc-warning",
                operation_id=operation.id,
                entity=operation.entity,
                kind="helical_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=target,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=cutter_radius,
                milling_direction=operation.milling_direction,
                moves=[],
                warnings=[
                    f"{operation.id}: tool diameter {tool.diameter:.6f} is too large for hole diameter {source_radius * 2:.6f}"
                ],
            )
        ]
    rough_offset = cutter_radius + finishing_allowance
    skip_roughing_for_fit = (
        operation.finishing.enabled
        and operation.finishing.side
        and 0 < finish_radius <= finishing_allowance
        and operation.skip_roughing_when_finish_fits
    )
    finish_paths = _offset_paths(source_path, "inside", cutter_radius)
    rough_paths = [] if skip_roughing_for_fit else _offset_paths(source_path, "inside", rough_offset)
    passes: list[ToolpathPass] = []
    warnings = []
    if skip_roughing_for_fit:
        warnings.append(
            f"{operation.id}: skipped roughing pass to accommodate selected tool; "
            f"finish radius {finish_radius:.6f} fits but requested roughing allowance {finishing_allowance:.6f} does not."
        )

    if rough_paths:
        rough_center, rough_radius = _circle_geometry_from_source_path(rough_paths[0])
        passes.append(
            ToolpathPass(
                id=f"{operation.id}-cavc-rough-helix",
                operation_id=operation.id,
                entity=operation.entity,
                kind="helical_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=target,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=rough_offset,
                milling_direction=operation.milling_direction,
                moves=_helix_moves(rough_center, rough_radius, target, operation.pitch, direction, safe_z, feed),
            )
        )

    if operation.finishing.enabled and operation.finishing.side:
        if not finish_paths:
            passes.append(
                ToolpathPass(
                    id=f"{operation.id}-cavc-finish",
                    operation_id=operation.id,
                    entity=operation.entity,
                    kind="finish_contour",
                    tool=operation.tool,
                    tool_diameter=tool.diameter,
                    feed_rate=feed,
                    z_top=0.0,
                    z_bottom=target,
                    source_path=source_path.id,
                    offset_side="inside",
                    offset_distance=cutter_radius,
                    milling_direction=operation.finishing.milling_direction,
                    moves=[],
                    warnings=[
                        f"{operation.id}: tool diameter {tool.diameter:.6f} leaves no machinable area for hole diameter {source_radius * 2:.6f}"
                    ],
                )
            )
            return passes
        finish_center, finish_radius = _circle_geometry_from_source_path(finish_paths[0])
        passes.append(
            ToolpathPass(
                id=f"{operation.id}-cavc-finish",
                operation_id=operation.id,
                entity=operation.entity,
                kind="finish_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=target,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=cutter_radius,
                milling_direction=operation.finishing.milling_direction,
                moves=_finish_circle_moves(finish_center, finish_radius, target, direction, safe_z, feed),
                warnings=warnings if skip_roughing_for_fit else [],
            )
        )
    return passes


def helical_pocket_operation_to_cavalier_toolpaths(
    operation: HelicalPocketOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    """Experimental circular helical pocket whose boundary radii come from Cavalier."""
    target = -operation.depth
    feed = operation.feed_rate or tool.feed_rate
    direction = "ccw" if operation.milling_direction == "climb" else "cw"
    cutter_radius = tool.diameter / 2
    rough_offset = cutter_radius
    if operation.finishing.enabled and operation.finishing.side:
        rough_offset += operation.roughing.side_allowance
    rough_paths = _offset_paths(source_path, "inside", rough_offset)
    finish_paths = _offset_paths(source_path, "inside", cutter_radius)
    passes: list[ToolpathPass] = []

    if operation.roughing.enabled and rough_paths:
        rough_center, rough_radius = _circle_geometry_from_source_path(rough_paths[0])
        moves = _helical_pocket_moves(
            center=rough_center,
            first_radius=min(tool.diameter / 2 * 0.95, rough_radius),
            final_radius=rough_radius,
            target_z=target,
            pitch=operation.pitch,
            stepover=tool.diameter * operation.stepover_percent / 100,
            direction=direction,
            safe_z=safe_z,
            feed=feed,
            retract=not (operation.finishing.enabled and operation.finishing.side),
        )
        passes.append(
            ToolpathPass(
                id=f"{operation.id}-cavc-rough-helical-pocket",
                operation_id=operation.id,
                entity=operation.entity,
                kind="helical_pocket",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=target,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=rough_offset,
                milling_direction=operation.milling_direction,
                moves=moves,
            )
        )

    if operation.finishing.enabled and operation.finishing.side and finish_paths:
        finish_center, finish_radius = _circle_geometry_from_source_path(finish_paths[0])
        passes.append(
            ToolpathPass(
                id=f"{operation.id}-cavc-finish",
                operation_id=operation.id,
                entity=operation.entity,
                kind="finish_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=feed,
                z_top=0.0,
                z_bottom=target,
                source_path=source_path.id,
                offset_side="inside",
                offset_distance=cutter_radius,
                milling_direction=operation.finishing.milling_direction,
                moves=_finish_circle_moves(
                    finish_center,
                    finish_radius,
                    target,
                    direction,
                    safe_z,
                    feed,
                    enter_at_safe_z=not passes,
                ),
            )
        )
    return passes


def boundary_raster_cavalier_toolpaths(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    """Experimental Kiri-style raster clearing with boundary/coastline links."""
    cutter_radius = tool.diameter / 2
    feed = operation.feed_rate or tool.feed_rate
    side_allowance = operation.roughing.side_allowance if operation.finishing.enabled and operation.finishing.side else 0.0
    bottom_allowance = operation.roughing.bottom_allowance if operation.finishing.enabled and operation.finishing.bottom else 0.0
    rough_depth = max(0.0, operation.depth - bottom_allowance)
    stepover = tool.diameter * operation.stepover_percent / 100
    passes: list[ToolpathPass] = []

    if operation.roughing.enabled and rough_depth > 1e-9:
        boundary_loops = offset_source_path(source_path, "inside", cutter_radius + side_allowance)
        raster_paths = _boundary_linked_raster_paths(boundary_loops, stepover)
        passes.append(
            _path_set_pass(
                operation=operation,
                source_path=source_path,
                tool=tool,
                kind="pocket_clear",
                pass_id=f"{operation.id}-cavc-boundary-raster",
                paths=raster_paths,
                depths=_depth_passes(rough_depth, operation.roughing.depth_per_pass, tool.depth_per_pass),
                feed=feed,
                safe_z=safe_z,
                offset_distance=cutter_radius + side_allowance,
            )
        )
    return passes


def _offset_clearing_pass(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    kind: str,
    pass_id: str,
    offset_distance: float,
    depths: list[float],
    feed: float,
    safe_z: float,
) -> ToolpathPass:
    stepover = tool.diameter * operation.stepover_percent / 100
    levels = _inward_offset_levels(source_path, offset_distance, stepover)
    loops = [loop for level in levels for loop in level]
    return _path_set_pass(
        operation=operation,
        source_path=source_path,
        tool=tool,
        kind=kind,
        pass_id=pass_id,
        paths=loops,
        depths=depths,
        feed=feed,
        safe_z=safe_z,
        offset_distance=offset_distance,
        link_paths=True,
        travel_boundaries=levels[0] if levels else [],
    )


def _offset_paths(source_path: SourcePath, offset_side: str, distance: float) -> list[SourcePath]:
    if offset_side == "on" or abs(distance) <= 1e-12:
        return [source_path]
    return offset_source_path(source_path, offset_side, abs(distance))


def _contour_offset_distance(offset_side: str, diameter: float, side_allowance: float) -> float:
    if offset_side == "on":
        return 0.0
    return diameter / 2 + side_allowance


def _circle_geometry_from_source_path(source_path: SourcePath) -> tuple[tuple[float, float], float]:
    arc_segments = [segment for segment in source_path.segments if isinstance(segment, SourceArcSegment)]
    if not arc_segments:
        raise ValueError(f"{source_path.id} is not circular enough for helical Cavalier output")
    center_x = sum(segment.center.x for segment in arc_segments) / len(arc_segments)
    center_y = sum(segment.center.y for segment in arc_segments) / len(arc_segments)
    radius = sum(segment.radius for segment in arc_segments) / len(arc_segments)
    max_center_error = max(math.hypot(segment.center.x - center_x, segment.center.y - center_y) for segment in arc_segments)
    max_radius_error = max(abs(segment.radius - radius) for segment in arc_segments)
    if max_center_error > 1e-5 or max_radius_error > 1e-5:
        raise ValueError(f"{source_path.id} does not have a stable circular offset")
    return (center_x, center_y), radius


def _has_active_tabs(operation: ContourOperation) -> bool:
    return bool(operation.tabs and operation.tabs.enabled and operation.tabs.locations)


def _path_set_pass(
    operation: PocketOperation,
    source_path: SourcePath,
    tool: Tool,
    kind: str,
    pass_id: str,
    paths: list[SourcePath],
    depths: list[float],
    feed: float,
    safe_z: float,
    offset_distance: float,
    link_paths: bool = False,
    travel_boundaries: list[SourcePath] | None = None,
) -> ToolpathPass:
    moves = []
    ramp_entry = operation.lead_in is None or operation.lead_in.type == "ramp"
    for depth in depths:
        z_bottom = -depth
        if link_paths:
            moves.extend(_linked_source_path_moves(paths, z_bottom, safe_z, feed, travel_boundaries or [], ramp_entry))
        else:
            for path in paths:
                if path.closed:
                    moves.extend(_closed_source_path_moves(path, z_bottom, safe_z, feed, ramp_entry=ramp_entry))
                else:
                    moves.extend(_open_source_path_moves(path, z_bottom, safe_z, feed))
    warnings = []
    if not paths:
        warnings.append(
            f"{operation.id}: pocket offset {offset_distance:.6f} leaves no machinable area for entity {operation.entity}"
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
        z_bottom=-depths[-1],
        source_path=source_path.id,
        offset_side="inside",
        offset_distance=offset_distance,
        milling_direction=operation.roughing.milling_direction,
        moves=moves,
        warnings=warnings,
    )


def _inward_offset_loops(
    source_path: SourcePath,
    offset_distance: float,
    stepover: float,
    max_loops: int = 40,
) -> list[SourcePath]:
    return [loop for level in _inward_offset_levels(source_path, offset_distance, stepover, max_loops) for loop in level]


def _inward_offset_levels(
    source_path: SourcePath,
    offset_distance: float,
    stepover: float,
    max_loops: int = 40,
) -> list[list[SourcePath]]:
    levels: list[list[SourcePath]] = []
    active = [source_path]
    distance = offset_distance
    for _index in range(max_loops):
        next_active: list[SourcePath] = []
        for path in active:
            try:
                next_active.extend(offset_source_path(path, "inside", distance if path is source_path else stepover))
            except CavalierUnavailable:
                raise
        if not next_active:
            break
        levels.append(next_active)
        active = next_active
        distance = stepover
    return levels


def _linked_source_path_moves(
    paths: list[SourcePath],
    z_bottom: float,
    safe_z: float,
    feed: float,
    travel_boundaries: list[SourcePath],
    ramp_entry: bool,
) -> list:
    """Kiri-style local path linking.

    Kiri keeps pocket output as independent contour/raster spans, then links
    nearby spans directly when the travel move remains inside the cuttable
    shadow. Otherwise it lifts and repositions. This follows that shape: order
    by nearest entry, rotate closed loops to that entry, and only cut-link when
    the connector stays inside the first tool-center boundary.
    """
    remaining = [path for path in paths if path.segments]
    if not remaining:
        return []

    travel_regions = _travel_region_polygons(travel_boundaries)
    moves = []
    current_xy: tuple[float, float] | None = None

    while remaining:
        index, path = _nearest_path_index(remaining, current_xy)
        path = remaining.pop(index)
        path = _orient_path_for_entry(path, current_xy)
        start = _path_start(path)
        if start is None:
            continue

        if current_xy is None:
            if ramp_entry and path.closed:
                moves.extend(_ramped_closed_source_path_moves(path, z_bottom, safe_z, feed, retract=False))
                current_xy = _path_end(path)
                continue
            moves.append(RapidMove(type="rapid", x=start[0], y=start[1], z=safe_z))
            moves.append(LineMove(type="line", z=z_bottom, feed=feed))
        elif _can_cut_link(current_xy, start, travel_regions):
            moves.append(LineMove(type="line", x=start[0], y=start[1], z=z_bottom, feed=feed))
        else:
            moves.append(RapidMove(type="rapid", z=safe_z))
            moves.append(RapidMove(type="rapid", x=start[0], y=start[1], z=safe_z))
            moves.append(LineMove(type="line", z=z_bottom, feed=feed))

        moves.extend(_source_path_cut_moves(path, z_bottom, feed))
        current_xy = _path_end(path)

    if moves:
        moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def _nearest_path_index(paths: list[SourcePath], current_xy: tuple[float, float] | None) -> tuple[int, SourcePath]:
    if current_xy is None:
        return 0, paths[0]
    best_index = 0
    best_distance = float("inf")
    for index, path in enumerate(paths):
        starts = [_path_start(path)]
        if not path.closed:
            starts.append(_path_end(path))
        for point in starts:
            if point is None:
                continue
            distance = _distance_sq(current_xy, point)
            if distance < best_distance:
                best_index = index
                best_distance = distance
    return best_index, paths[best_index]


def _orient_path_for_entry(path: SourcePath, current_xy: tuple[float, float] | None) -> SourcePath:
    if current_xy is None:
        return path
    if path.closed:
        return _rotate_closed_path_to_nearest_start(path, current_xy)
    start = _path_start(path)
    end = _path_end(path)
    if start is not None and end is not None and _distance_sq(current_xy, end) < _distance_sq(current_xy, start):
        return _reverse_path(path)
    return path


def _rotate_closed_path_to_nearest_start(path: SourcePath, target: tuple[float, float]) -> SourcePath:
    if not path.segments:
        return path
    index = min(range(len(path.segments)), key=lambda segment_index: _distance_sq(target, _segment_start(path.segments[segment_index])))
    if index == 0:
        return path
    return SourcePath(
        id=path.id,
        entity=path.entity,
        closed=path.closed,
        segments=[*path.segments[index:], *path.segments[:index]],
    )


def _reverse_path(path: SourcePath) -> SourcePath:
    return SourcePath(
        id=path.id,
        entity=path.entity,
        closed=path.closed,
        segments=[_reverse_segment(segment) for segment in reversed(path.segments)],
    )


def _reverse_segment(segment: SourceLineSegment | SourceArcSegment) -> SourceLineSegment | SourceArcSegment:
    if isinstance(segment, SourceLineSegment):
        return SourceLineSegment(type="line", start=segment.end, end=segment.start)
    return SourceArcSegment(
        type="arc",
        start=segment.end,
        end=segment.start,
        center=segment.center,
        radius=segment.radius,
        direction="cw" if segment.direction == "ccw" else "ccw",
    )


def _source_path_cut_moves(source_path: SourcePath, z_bottom: float, feed: float) -> list:
    moves = []
    for segment in source_path.segments:
        if isinstance(segment, SourceLineSegment):
            moves.append(LineMove(type="line", x=segment.end.x, y=segment.end.y, z=z_bottom, feed=feed))
        elif isinstance(segment, SourceArcSegment):
            moves.append(
                ArcMove(
                    type="arc",
                    direction=segment.direction,
                    x=segment.end.x,
                    y=segment.end.y,
                    z=z_bottom,
                    i=segment.center.x - segment.start.x,
                    j=segment.center.y - segment.start.y,
                    feed=feed,
                )
            )
    return moves


def _path_start(path: SourcePath) -> tuple[float, float] | None:
    if not path.segments:
        return None
    return _segment_start(path.segments[0])


def _path_end(path: SourcePath) -> tuple[float, float] | None:
    if not path.segments:
        return None
    end = path.segments[-1].end
    return end.x, end.y


def _segment_start(segment: SourceLineSegment | SourceArcSegment) -> tuple[float, float]:
    return segment.start.x, segment.start.y


def _travel_region_polygons(boundaries: list[SourcePath]) -> list[Polygon]:
    regions = []
    for boundary in boundaries:
        points = source_path_points(boundary, arc_segments=96)
        if len(points) < 3:
            continue
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            regions.append(polygon)
    return regions


def _can_cut_link(start: tuple[float, float], end: tuple[float, float], travel_regions: list[Polygon]) -> bool:
    if _same_point(start, end):
        return True
    if not travel_regions:
        return False
    connector = LineString([start, end])
    return any(region.buffer(1e-9).covers(connector) for region in travel_regions)


def _closed_source_path_moves(source_path: SourcePath, z_bottom: float, safe_z: float, feed: float, ramp_entry: bool = False) -> list:
    if not source_path.segments:
        return []
    if ramp_entry:
        return _ramped_closed_source_path_moves(source_path, z_bottom, safe_z, feed, retract=True)
    start = source_path.segments[0].start
    moves = [RapidMove(type="rapid", x=start.x, y=start.y, z=safe_z), LineMove(type="line", z=z_bottom, feed=feed)]
    for segment in source_path.segments:
        if isinstance(segment, SourceLineSegment):
            moves.append(LineMove(type="line", x=segment.end.x, y=segment.end.y, z=z_bottom, feed=feed))
        elif isinstance(segment, SourceArcSegment):
            moves.append(
                ArcMove(
                    type="arc",
                    direction=segment.direction,
                    x=segment.end.x,
                    y=segment.end.y,
                    z=z_bottom,
                    i=segment.center.x - segment.start.x,
                    j=segment.center.y - segment.start.y,
                    feed=feed,
                )
            )
    moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def _ramped_closed_source_path_moves(
    source_path: SourcePath,
    z_bottom: float,
    safe_z: float,
    feed: float,
    *,
    retract: bool,
) -> list:
    if not source_path.closed or not source_path.segments:
        return _closed_source_path_moves(source_path, z_bottom, safe_z, feed, ramp_entry=False)
    start = source_path.segments[0].start
    moves = [
        RapidMove(type="rapid", x=start.x, y=start.y, z=safe_z),
        LineMove(type="line", z=0.0, feed=feed),
    ]
    path_length = max(_source_path_length(source_path), 1e-9)
    traveled = 0.0
    for segment in source_path.segments:
        traveled += _segment_length(segment)
        z = z_bottom * min(1.0, traveled / path_length)
        moves.extend(_segment_cut_move(segment, z, feed))
    moves.extend(_source_path_cut_moves(source_path, z_bottom, feed))
    if retract:
        moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def _ramped_source_path_moves(
    source_path: SourcePath,
    depths: list[float],
    safe_z: float,
    feed: float,
    *,
    bottom_cleanup: bool,
) -> list:
    if not source_path.segments or not depths:
        return []
    start = source_path.segments[0].start
    moves = [
        RapidMove(type="rapid", x=start.x, y=start.y, z=safe_z),
        LineMove(type="line", z=0.0, feed=feed),
    ]
    path_length = max(_source_path_length(source_path), 1e-9)
    current_depth = 0.0
    for target_depth in depths:
        traveled = 0.0
        for segment in source_path.segments:
            traveled += _segment_length(segment)
            fraction = min(1.0, traveled / path_length)
            z = -(current_depth + (target_depth - current_depth) * fraction)
            moves.extend(_segment_cut_move(segment, z, feed))
        current_depth = target_depth
    if bottom_cleanup:
        moves.extend(_source_path_cut_moves(source_path, -depths[-1], feed))
    moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def _source_path_length(source_path: SourcePath) -> float:
    return sum(_segment_length(segment) for segment in source_path.segments)


def _segment_length(segment: SourceLineSegment | SourceArcSegment) -> float:
    if isinstance(segment, SourceLineSegment):
        return math.hypot(segment.end.x - segment.start.x, segment.end.y - segment.start.y)
    return abs(_arc_sweep(segment)) * segment.radius


def _arc_sweep(segment: SourceArcSegment) -> float:
    start_angle = math.atan2(segment.start.y - segment.center.y, segment.start.x - segment.center.x)
    end_angle = math.atan2(segment.end.y - segment.center.y, segment.end.x - segment.center.x)
    sweep = end_angle - start_angle
    if segment.direction == "ccw" and sweep <= 0:
        sweep += 2 * math.pi
    if segment.direction == "cw" and sweep >= 0:
        sweep -= 2 * math.pi
    return sweep


def _segment_cut_move(segment: SourceLineSegment | SourceArcSegment, z: float, feed: float) -> list:
    if isinstance(segment, SourceLineSegment):
        return [LineMove(type="line", x=segment.end.x, y=segment.end.y, z=z, feed=feed)]
    return [
        ArcMove(
            type="arc",
            direction=segment.direction,
            x=segment.end.x,
            y=segment.end.y,
            z=z,
            i=segment.center.x - segment.start.x,
            j=segment.center.y - segment.start.y,
            feed=feed,
        )
    ]


def _open_source_path_moves(source_path: SourcePath, z_bottom: float, safe_z: float, feed: float) -> list:
    if not source_path.segments:
        return []
    start = source_path.segments[0].start
    moves = [RapidMove(type="rapid", x=start.x, y=start.y, z=safe_z), LineMove(type="line", z=z_bottom, feed=feed)]
    for segment in source_path.segments:
        if isinstance(segment, SourceLineSegment):
            moves.append(LineMove(type="line", x=segment.end.x, y=segment.end.y, z=z_bottom, feed=feed))
        elif isinstance(segment, SourceArcSegment):
            moves.append(
                ArcMove(
                    type="arc",
                    direction=segment.direction,
                    x=segment.end.x,
                    y=segment.end.y,
                    z=z_bottom,
                    i=segment.center.x - segment.start.x,
                    j=segment.center.y - segment.start.y,
                    feed=feed,
                )
            )
    moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def _boundary_linked_raster_paths(boundary_loops: list[SourcePath], stepover: float) -> list[SourcePath]:
    paths: list[SourcePath] = []
    for loop_index, boundary in enumerate(boundary_loops, start=1):
        boundary_points = source_path_points(boundary, arc_segments=96)
        if len(boundary_points) < 3:
            continue
        polygon = Polygon(boundary_points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        row_segments = _raster_segments(polygon, stepover)
        if not row_segments:
            continue
        linked_points = _link_raster_segments(row_segments, boundary_points, polygon)
        if len(linked_points) >= 2:
            paths.append(_open_polyline_source_path(f"{boundary.id}-boundary-raster-{loop_index}", boundary.entity, linked_points))
    return paths


def _raster_segments(polygon: Polygon, stepover: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    min_x, min_y, max_x, max_y = polygon.bounds
    rows = []
    row_index = 0
    y = max_y - stepover / 2
    while y >= min_y + stepover / 2 - 1e-9:
        line = LineString([(min_x - stepover, y), (max_x + stepover, y)])
        intersections = _lines(polygon.intersection(line))
        intersections.sort(key=lambda segment: segment.coords[0][0])
        if row_index % 2:
            intersections.reverse()
        for segment in intersections:
            coords = [(float(x), float(y)) for x, y in segment.coords]
            if len(coords) < 2 or segment.length <= 1e-9:
                continue
            start = coords[0]
            end = coords[-1]
            if row_index % 2:
                start, end = end, start
            rows.append((start, end))
        row_index += 1
        y -= stepover
    return rows


def _lines(geometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return [line for item in getattr(geometry, "geoms", []) for line in _lines(item)]


def _link_raster_segments(
    row_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    boundary_points: list[tuple[float, float]],
    polygon: Polygon,
) -> list[tuple[float, float]]:
    points = [row_segments[0][0], row_segments[0][1]]
    current = row_segments[0][1]
    for start, end in row_segments[1:]:
        points.extend(_safe_connector(current, start, boundary_points, polygon)[1:])
        points.append(end)
        current = end
    return _dedupe_points(points)


def _safe_connector(
    start: tuple[float, float],
    end: tuple[float, float],
    boundary_points: list[tuple[float, float]],
    polygon: Polygon,
) -> list[tuple[float, float]]:
    direct = LineString([start, end])
    if polygon.buffer(1e-9).covers(direct):
        return [start, end]
    loop = boundary_points[:-1] if boundary_points and _same_point(boundary_points[0], boundary_points[-1]) else boundary_points
    if len(loop) < 2:
        return [start, end]
    start_index = min(range(len(loop)), key=lambda index: _distance_sq(start, loop[index]))
    end_index = min(range(len(loop)), key=lambda index: _distance_sq(end, loop[index]))
    forward = _boundary_slice(loop, start_index, end_index, 1)
    backward = _boundary_slice(loop, start_index, end_index, -1)
    route = forward if _polyline_length(forward) <= _polyline_length(backward) else backward
    return [start, *route, end]


def _boundary_slice(
    loop: list[tuple[float, float]],
    start_index: int,
    end_index: int,
    step: int,
) -> list[tuple[float, float]]:
    route = [loop[start_index]]
    index = start_index
    guard = 0
    while index != end_index and guard <= len(loop):
        index = (index + step) % len(loop)
        route.append(loop[index])
        guard += 1
    return route


def _open_polyline_source_path(source_id: str, entity: str | None, points: list[tuple[float, float]]) -> SourcePath:
    points = _dedupe_points(points)
    return SourcePath(
        id=source_id,
        entity=entity or source_id,
        closed=False,
        segments=[
            SourceLineSegment(
                type="line",
                start=_point(start),
                end=_point(end),
            )
            for start, end in zip(points, points[1:], strict=False)
            if not _same_point(start, end)
        ],
    )


def _point(point: tuple[float, float]):
    from dxfwiz.schemas.common import Point2D

    return Point2D(x=point[0], y=point[1])


def _dedupe_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in points:
        if not result or not _same_point(result[-1], point):
            result.append(point)
    return result


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(math.sqrt(_distance_sq(first, second)) for first, second in zip(points, points[1:], strict=False))


def _distance_sq(first: tuple[float, float], second: tuple[float, float]) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _same_point(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return _distance_sq(first, second) <= 1e-14
