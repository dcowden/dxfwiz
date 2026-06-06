from __future__ import annotations

import math
from html import escape
from statistics import median
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon

from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import ContourOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import (
    LineMove,
    RapidMove,
    SourceArcSegment,
    SourceLineSegment,
    SourcePath,
    ToolpathPass,
)


def contour_operation_to_toolpaths(
    operation: ContourOperation,
    source_path: SourcePath,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    """Translate one contour operation into rough and finish contour passes.

    This follows the same high-level pattern Kiri:Moto uses for outline/trace work:
    rough passes are offset by cutter radius plus leave stock, and finishing passes
    use cutter radius only.
    """
    passes: list[ToolpathPass] = []
    total_depth = operation.depth + operation.extra_depth
    if operation.roughing.enabled:
        rough_offset = _offset_distance(operation.offset, tool.diameter, operation.roughing.side_allowance)
        rough_points = offset_source_path(source_path, operation.offset, rough_offset)
        depths = _depth_passes(operation.depth, operation.roughing.depth_per_pass, tool.depth_per_pass)
        depths = _add_extra_depth_to_final_pass(depths, operation.extra_depth, tool.depth_per_pass)
        if not rough_points:
            passes.append(
                _unmachinable_contour_pass(
                    operation=operation,
                    source_path=source_path,
                    tool=tool,
                    kind="rough_contour",
                    pass_id=f"{operation.id}-rough",
                    z_bottom=-depths[-1],
                    offset_distance=abs(rough_offset),
                )
            )
        if operation.ramping:
            z_bottom = -depths[-1]
            has_finish_contour = operation.finishing.enabled and operation.finishing.side
            if rough_points:
                passes.append(
                    ToolpathPass(
                        id=f"{operation.id}-rough-spiral",
                        operation_id=operation.id,
                        entity=operation.entity,
                        kind="rough_contour",
                        tool=operation.tool,
                        tool_diameter=tool.diameter,
                        z_top=0.0,
                        z_bottom=z_bottom,
                        source_path=source_path.id,
                        offset_side=operation.offset,
                        offset_distance=abs(rough_offset),
                        milling_direction=operation.roughing.milling_direction,
                        moves=_spiral_line_moves(
                            rough_points,
                            depths,
                            safe_z,
                            operation.feed_rate or tool.feed_rate,
                            bottom_cleanup=not has_finish_contour,
                        ),
                    )
                )
        elif rough_points:
            z_bottom = -depths[-1]
            passes.append(
                ToolpathPass(
                    id=f"{operation.id}-rough",
                    operation_id=operation.id,
                    entity=operation.entity,
                    kind="rough_contour",
                    tool=operation.tool,
                    tool_diameter=tool.diameter,
                    z_top=0.0,
                    z_bottom=z_bottom,
                    source_path=source_path.id,
                    offset_side=operation.offset,
                    offset_distance=abs(rough_offset),
                    milling_direction=operation.roughing.milling_direction,
                    moves=_layered_line_moves(rough_points, depths, safe_z, operation.feed_rate or tool.feed_rate),
                )
            )

    if operation.finishing.enabled and operation.finishing.side:
        finish_offset = _offset_distance(operation.offset, tool.diameter, 0.0)
        finish_points = offset_source_path(source_path, operation.offset, finish_offset)
        for index in range(1, operation.finishing.passes + 1):
            z_bottom = -total_depth
            if not finish_points:
                passes.append(
                    _unmachinable_contour_pass(
                        operation=operation,
                        source_path=source_path,
                        tool=tool,
                        kind="finish_contour",
                        pass_id=f"{operation.id}-finish-{index}",
                        z_bottom=z_bottom,
                        offset_distance=abs(finish_offset),
                    )
                )
                continue
            passes.append(
                ToolpathPass(
                    id=f"{operation.id}-finish-{index}",
                    operation_id=operation.id,
                    entity=operation.entity,
                    kind="finish_contour",
                    tool=operation.tool,
                    tool_diameter=tool.diameter,
                    z_top=0.0,
                    z_bottom=z_bottom,
                    source_path=source_path.id,
                    offset_side=operation.offset,
                    offset_distance=abs(finish_offset),
                    milling_direction=operation.finishing.milling_direction,
                    moves=_closed_line_moves(finish_points, z_bottom, safe_z, operation.feed_rate or tool.feed_rate),
                )
            )

    return passes


def offset_source_path(
    source_path: SourcePath,
    offset_side: str,
    distance: float,
) -> list[tuple[float, float]]:
    points = source_path_points(source_path)
    if offset_side == "on" or abs(distance) <= 1e-12:
        return points
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.area <= 0:
        polygon = polygon.buffer(0)
    signed_distance = distance if offset_side == "outside" else -distance
    offset = polygon.buffer(signed_distance, join_style="round")
    if offset.is_empty:
        return []
    if offset.geom_type == "MultiPolygon":
        offset = max(offset.geoms, key=lambda item: item.area)
    return [(float(x), float(y)) for x, y in offset.exterior.coords[:-1]]


def source_path_points(source_path: SourcePath, arc_segments: int = 48) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in source_path.segments:
        if isinstance(segment, SourceLineSegment):
            _append_unique(points, _xy(segment.start))
            _append_unique(points, _xy(segment.end))
        elif isinstance(segment, SourceArcSegment):
            arc_points = _arc_points(segment, arc_segments)
            for point in arc_points:
                _append_unique(points, point)
    if source_path.closed and len(points) > 1 and _same_point(points[0], points[-1]):
        points.pop()
    return points


def offset_distance_statistics(
    source_path: SourcePath,
    toolpath_pass: ToolpathPass,
) -> dict[str, float]:
    source_points = source_path_points(source_path)
    source_line = LineString([*source_points, source_points[0]])
    cut_points: list[tuple[float, float]] = [
        (move.x, move.y)
        for move in toolpath_pass.moves
        if isinstance(move, LineMove) and move.x is not None and move.y is not None
    ]
    sample_points = []
    for first, second in zip(cut_points, cut_points[1:], strict=False):
        for fraction in (0.2, 0.4, 0.6, 0.8):
            sample_points.append(
                (
                    first[0] + (second[0] - first[0]) * fraction,
                    first[1] + (second[1] - first[1]) * fraction,
                )
            )
    distances = [source_line.distance(Point(point)) for point in sample_points]
    if not distances:
        return {"min": 0.0, "max": 0.0, "median": 0.0}
    return {"min": min(distances), "max": max(distances), "median": median(distances)}


def assert_mostly_offset(
    source_path: SourcePath,
    toolpath_pass: ToolpathPass,
    expected_distance: float,
    tolerance: float = 1e-3,
) -> None:
    stats = offset_distance_statistics(source_path, toolpath_pass)
    if abs(stats["median"] - expected_distance) > tolerance:
        raise AssertionError(
            f"Median offset distance {stats['median']:.6f} is not within {tolerance} "
            f"of expected {expected_distance:.6f}. Stats: {stats}"
        )


def _unmachinable_contour_pass(
    operation: ContourOperation,
    source_path: SourcePath,
    tool: Tool,
    kind: str,
    pass_id: str,
    z_bottom: float,
    offset_distance: float,
) -> ToolpathPass:
    return ToolpathPass(
        id=pass_id,
        operation_id=operation.id,
        entity=operation.entity,
        kind=kind,
        tool=operation.tool,
        tool_diameter=tool.diameter,
        z_top=0.0,
        z_bottom=z_bottom,
        source_path=source_path.id,
        offset_side=operation.offset,
        offset_distance=offset_distance,
        milling_direction=operation.roughing.milling_direction,
        moves=[],
        warnings=[
            f"{operation.id}: {kind} offset {offset_distance:.6f} cannot be machined for entity {operation.entity}",
        ],
    )


def render_toolpath_preview_svg(
    source_path: SourcePath,
    passes: list[ToolpathPass],
    output_path: str | Path,
    width: int = 1200,
    height: int = 800,
) -> str:
    svg = render_toolpath_preview_sheet_svg(
        [(source_path.entity, source_path, passes)],
        output_path,
        width=width,
        row_height=height,
    )
    return svg


def render_toolpath_preview_sheet_svg(
    previews: list[tuple[str, SourcePath, list[ToolpathPass]]],
    output_path: str | Path,
    width: int = 1400,
    row_height: int = 520,
) -> str:
    height = max(row_height * len(previews), row_height)
    legend_width = 260
    top_width = (width - legend_width) // 2
    iso_width = width - legend_width - top_width
    colors = {
        "rough_contour": "#2563eb",
        "finish_contour": "#dc2626",
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text { font-family: Arial, sans-serif; font-size: 15px; fill: #111827; }",
        ".title { font-weight: 700; font-size: 18px; }",
        ".meta { fill: #4b5563; font-size: 13px; }",
        ".source { fill: none; stroke: #111827; stroke-width: 3; }",
        ".source-iso { fill: none; stroke: #111827; stroke-width: 2; opacity: 0.45; }",
        ".path { fill: none; stroke-width: 2.5; }",
        ".path-iso { fill: none; stroke-width: 2.5; }",
        ".finish { stroke-dasharray: 8 5; }",
        ".dot { stroke: white; stroke-width: 2; }",
        ".panel-label { font-weight: 700; fill: #374151; }",
        ".divider { stroke: #d1d5db; stroke-width: 1; }",
        "</style>",
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff" />',
        f'<text class="panel-label" x="{top_width + iso_width + 24}" y="36">Legend</text>',
        f'<line x1="{top_width + iso_width + 24}" y1="68" x2="{top_width + iso_width + 54}" y2="68" stroke="#111827" stroke-width="4" />',
        f'<text x="{top_width + iso_width + 66}" y="74">source contour</text>',
        f'<line x1="{top_width + iso_width + 24}" y1="100" x2="{top_width + iso_width + 54}" y2="100" stroke="#2563eb" stroke-width="4" />',
        f'<text x="{top_width + iso_width + 66}" y="106">rough path</text>',
        f'<line x1="{top_width + iso_width + 24}" y1="132" x2="{top_width + iso_width + 54}" y2="132" stroke="#dc2626" stroke-width="4" stroke-dasharray="8 5" />',
        f'<text x="{top_width + iso_width + 66}" y="138">finish path</text>',
    ]
    for row_index, (title, source_path, passes) in enumerate(previews):
        lines.extend(
            _toolpath_preview_row_svg(
                title=title,
                source_path=source_path,
                passes=passes,
                colors=colors,
                row_y=row_index * row_height,
                row_height=row_height,
                top_width=top_width,
                iso_width=iso_width,
                legend_x=top_width + iso_width,
            )
        )
    lines.append("</svg>")
    svg = "\n".join(lines) + "\n"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(svg, encoding="utf-8")
    return svg


def _toolpath_preview_row_svg(
    title: str,
    source_path: SourcePath,
    passes: list[ToolpathPass],
    colors: dict[str, str],
    row_y: int,
    row_height: int,
    top_width: int,
    iso_width: int,
    legend_x: int,
) -> list[str]:
    source_points = source_path_points(source_path)
    pass_xyz_points = [_toolpath_xyz_points(toolpath_pass) for toolpath_pass in passes]
    pass_points = [[(x, y) for x, y, _z in points] for points in pass_xyz_points]
    all_points = [*source_points, *(point for points in pass_points for point in points)]
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    drawing_height = row_height - 110
    margin = 60
    model_width = max(max_x - min_x, 1e-9)
    model_height = max(max_y - min_y, 1e-9)
    scale = min((top_width - 2 * margin) / model_width, (drawing_height - 2 * margin) / model_height)
    scaled_width = model_width * scale
    scaled_height = model_height * scale
    origin_x = margin + (top_width - 2 * margin - scaled_width) / 2
    origin_y = margin + (drawing_height - 2 * margin - scaled_height) / 2

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        return origin_x + (point[0] - min_x) * scale, row_y + origin_y + (max_y - point[1]) * scale

    iso_projected_sets = [
        [_iso_project(x, y, z) for x, y, z in points]
        for points in pass_xyz_points
    ]
    iso_source = [_iso_project(point[0], point[1], 0.0) for point in source_points]
    iso_all = [*iso_source, *(point for points in iso_projected_sets for point in points)]
    iso_min_x = min(point[0] for point in iso_all)
    iso_max_x = max(point[0] for point in iso_all)
    iso_min_y = min(point[1] for point in iso_all)
    iso_max_y = max(point[1] for point in iso_all)
    iso_model_width = max(iso_max_x - iso_min_x, 1e-9)
    iso_model_height = max(iso_max_y - iso_min_y, 1e-9)
    iso_scale = min((iso_width - 2 * margin) / iso_model_width, (drawing_height - 2 * margin) / iso_model_height)
    iso_origin_x = top_width + margin + (iso_width - 2 * margin - iso_model_width * iso_scale) / 2
    iso_origin_y = margin + (drawing_height - 2 * margin - iso_model_height * iso_scale) / 2

    def iso_screen(point: tuple[float, float]) -> tuple[float, float]:
        return iso_origin_x + (point[0] - iso_min_x) * iso_scale, row_y + iso_origin_y + (point[1] - iso_min_y) * iso_scale

    source_polyline = _svg_polyline([screen(point) for point in source_points], close=True)
    iso_source_polyline = _svg_polyline([iso_screen(point) for point in iso_source], close=True)
    lines = [
        f'<line class="divider" x1="0" y1="{row_y}" x2="{legend_x}" y2="{row_y}" />' if row_y else "",
        f'<text class="title" x="24" y="{row_y + 32}">{escape(title)}</text>',
        f'<text class="meta" x="24" y="{row_y + 54}">model extents: {model_width:.3f} x {model_height:.3f}</text>',
        f'<text class="panel-label" x="24" y="{row_y + 86}">Top view</text>',
        f'<text class="panel-label" x="{top_width + 24}" y="{row_y + 86}">Isometric depth view</text>',
        f'<polyline class="source" points="{source_polyline}" />',
        f'<polyline class="source-iso" points="{iso_source_polyline}" />',
    ]
    for index, (toolpath_pass, points, iso_points) in enumerate(
        zip(passes, pass_points, iso_projected_sets, strict=True),
        start=1,
    ):
        color = colors.get(toolpath_pass.kind, "#7c3aed")
        screen_points = [screen(point) for point in points]
        iso_screen_points = [iso_screen(point) for point in iso_points]
        css_class = "path finish" if toolpath_pass.kind == "finish_contour" else "path"
        iso_css_class = "path-iso finish" if toolpath_pass.kind == "finish_contour" else "path-iso"
        lines.append(
            f'<polyline class="{css_class}" data-pass="{escape(toolpath_pass.id)}" '
            f'stroke="{color}" points="{_svg_polyline(screen_points, close=False)}" />'
        )
        lines.append(
            f'<polyline class="{iso_css_class}" data-pass="{escape(toolpath_pass.id)}-iso" '
            f'stroke="{color}" points="{_svg_polyline(iso_screen_points, close=False)}" />'
        )
        if screen_points:
            x, y = screen_points[0]
            lines.append(f'<circle class="dot" cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}" />')
            lines.extend(_arrow_markers(screen_points, color))
        if iso_screen_points:
            lines.extend(_arrow_markers(iso_screen_points, color))
        label_x = legend_x + 24
        label_y = row_y + 165 + index * 25
        lines.append(f'<line x1="{label_x}" y1="{label_y - 5}" x2="{label_x + 22}" y2="{label_y - 5}" stroke="{color}" stroke-width="4" />')
        lines.append(
            f'<text x="{label_x + 34}" y="{label_y}">{index}: {escape(toolpath_pass.kind)} '
            f'z {toolpath_pass.z_bottom:.3f}</text>'
        )
    return [line for line in lines if line]


def _toolpath_xyz_points(toolpath_pass: ToolpathPass) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    current_x: float | None = None
    current_y: float | None = None
    current_z: float | None = None
    cutting = False
    for move in toolpath_pass.moves:
        if isinstance(move, RapidMove):
            if move.x is not None:
                current_x = move.x
            if move.y is not None:
                current_y = move.y
            if move.z is not None:
                current_z = move.z
            cutting = False
            continue
        if not isinstance(move, LineMove):
            continue
        previous = (current_x, current_y, current_z)
        if move.x is not None:
            current_x = move.x
        if move.y is not None:
            current_y = move.y
        if move.z is not None:
            current_z = move.z
        if current_x is None or current_y is None or current_z is None:
            continue
        if not cutting:
            if previous[0] is not None and previous[1] is not None and previous[2] is not None:
                points.append((previous[0], previous[1], previous[2]))
            points.append((current_x, current_y, current_z))
            cutting = True
            continue
        points.append((current_x, current_y, current_z))
    return points


def _arrow_markers(points: list[tuple[float, float]], color: str) -> list[str]:
    if len(points) < 2:
        return []
    candidate_indices = [1]
    if len(points) > 5:
        candidate_indices.extend([len(points) // 3, (len(points) * 2) // 3])
    candidate_indices.append(len(points) - 1)
    lines = []
    used: set[int] = set()
    for index in candidate_indices:
        index = max(1, min(index, len(points) - 1))
        if index in used:
            continue
        used.add(index)
        arrow = _arrow_polygon(points[index - 1], points[index], size=11)
        if arrow:
            lines.append(f'<polygon points="{arrow}" fill="{color}" opacity="0.92" />')
    return lines


def _arrow_polygon(start: tuple[float, float], end: tuple[float, float], size: float) -> str | None:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    ux = dx / length
    uy = dy / length
    px = -uy
    py = ux
    tip = end
    base_x = end[0] - ux * size
    base_y = end[1] - uy * size
    half_width = size * 0.42
    left = (base_x + px * half_width, base_y + py * half_width)
    right = (base_x - px * half_width, base_y - py * half_width)
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in (tip, left, right))


def _iso_project(x: float, y: float, z: float) -> tuple[float, float]:
    return (x - y) * 0.8660254038, (x + y) * 0.5 - z * 3.0


def _offset_distance(offset_side: str, diameter: float, side_allowance: float) -> float:
    if offset_side == "on":
        return 0.0
    return diameter / 2 + side_allowance


def _closed_line_moves(
    points: list[tuple[float, float]],
    z_bottom: float,
    safe_z: float,
    feed: float,
) -> list[dict]:
    if not points:
        return []
    start = points[0]
    moves: list[dict] = [
        {"type": "rapid", "x": start[0], "y": start[1], "z": safe_z},
        {"type": "line", "z": z_bottom, "feed": feed},
    ]
    moves.extend({"type": "line", "x": x, "y": y, "z": z_bottom, "feed": feed} for x, y in points[1:])
    moves.append({"type": "line", "x": start[0], "y": start[1], "z": z_bottom, "feed": feed})
    return moves


def _layered_line_moves(
    points: list[tuple[float, float]],
    depths: list[float],
    safe_z: float,
    feed: float,
) -> list[dict]:
    if not points or not depths:
        return []
    start = points[0]
    moves: list[dict] = [{"type": "rapid", "x": start[0], "y": start[1], "z": safe_z}]
    for depth in depths:
        z_bottom = -depth
        moves.append({"type": "line", "z": z_bottom, "feed": feed})
        moves.extend({"type": "line", "x": x, "y": y, "z": z_bottom, "feed": feed} for x, y in points[1:])
        moves.append({"type": "line", "x": start[0], "y": start[1], "z": z_bottom, "feed": feed})
    return moves


def _spiral_line_moves(
    points: list[tuple[float, float]],
    depths: list[float],
    safe_z: float,
    feed: float,
    bottom_cleanup: bool,
) -> list[dict]:
    if not points or not depths:
        return []
    start = points[0]
    closed_points = [*points, start]
    moves: list[dict] = [
        {"type": "rapid", "x": start[0], "y": start[1], "z": safe_z},
        {"type": "line", "z": 0.0, "feed": feed},
    ]
    current_depth = 0.0
    for target_depth in depths:
        loop_segments = len(closed_points) - 1
        for index, (x, y) in enumerate(closed_points[1:], start=1):
            fraction = index / loop_segments
            depth = current_depth + (target_depth - current_depth) * fraction
            moves.append({"type": "line", "x": x, "y": y, "z": -depth, "feed": feed})
        current_depth = target_depth
    if bottom_cleanup:
        z_bottom = -depths[-1]
        moves.extend({"type": "line", "x": x, "y": y, "z": z_bottom, "feed": feed} for x, y in closed_points[1:])
    return moves


def _depth_passes(depth: float, step: float, hard_step: float | None = None) -> list[float]:
    hard_step = hard_step or step
    step = min(step, hard_step)
    passes: list[float] = []
    current_depth = 0.0
    while current_depth < depth - 1e-9:
        next_depth = min(current_depth + step, depth)
        if current_depth > 0 and depth - current_depth <= hard_step + 1e-9:
            next_depth = depth
        passes.append(next_depth)
        current_depth = next_depth
    return passes


def _add_extra_depth_to_final_pass(
    depths: list[float],
    extra_depth: float,
    hard_step: float | None,
) -> list[float]:
    if not depths or extra_depth <= 0:
        return depths
    hard_step = hard_step or (depths[-1] - (depths[-2] if len(depths) > 1 else 0.0))
    previous_depth = depths[-2] if len(depths) > 1 else 0.0
    final_depth = depths[-1] + extra_depth
    if final_depth - previous_depth <= hard_step + 1e-9:
        return [*depths[:-1], final_depth]
    return [*depths, final_depth]


def _arc_points(segment: SourceArcSegment, arc_segments: int) -> list[tuple[float, float]]:
    start_angle = math.atan2(segment.start.y - segment.center.y, segment.start.x - segment.center.x)
    end_angle = math.atan2(segment.end.y - segment.center.y, segment.end.x - segment.center.x)
    sweep = end_angle - start_angle
    if segment.direction == "ccw" and sweep <= 0:
        sweep += 2 * math.pi
    if segment.direction == "cw" and sweep >= 0:
        sweep -= 2 * math.pi
    steps = max(2, math.ceil(abs(sweep) / (2 * math.pi) * arc_segments))
    return [
        (
            segment.center.x + math.cos(start_angle + sweep * index / steps) * segment.radius,
            segment.center.y + math.sin(start_angle + sweep * index / steps) * segment.radius,
        )
        for index in range(steps + 1)
    ]


def _xy(point: Point2D) -> tuple[float, float]:
    return point.x, point.y


def _append_unique(points: list[tuple[float, float]], point: tuple[float, float]) -> None:
    if not points or not _same_point(points[-1], point):
        points.append(point)


def _same_point(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return abs(first[0] - second[0]) <= 1e-9 and abs(first[1] - second[1]) <= 1e-9


def _svg_polyline(points: list[tuple[float, float]], close: bool) -> str:
    output = points
    if close and points:
        output = [*points, points[0]]
    return " ".join(f"{x:.6f},{y:.6f}" for x, y in output)
