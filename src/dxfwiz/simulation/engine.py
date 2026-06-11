from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from dxfwiz.schemas.machine import Tool
from dxfwiz.simulation.dexel import DexelGrid
from dxfwiz.simulation.model import (
    DexelSimulationRequest,
    DexelSimulationResponse,
    SimulationIssue,
    SimulationMetrics,
    ToolProfile,
)
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove


@dataclass
class DexelSimulationRun:
    response: DexelSimulationResponse
    grid: DexelGrid


def simulate_toolpath(request: DexelSimulationRequest, include_snapshot: bool = False) -> DexelSimulationRun:
    tools = {tool.id: tool for tool in request.machine.tools}
    minimum_tool_diameter = _minimum_tool_diameter(request, tools)
    grid = DexelGrid.create(request.stock, request.settings, minimum_tool_diameter)
    _apply_expected_removals(grid, request)
    issues: list[SimulationIssue] = []
    air_cut_moves = 0
    rapid_collision_count = 0
    unsafe_rapid_count = 0
    position = (0.0, 0.0, request.machine.machine.clear_z)

    for toolpath_pass in request.toolpath_plan.passes:
        tool = tools.get(toolpath_pass.tool or "")
        profile = _tool_profile(toolpath_pass.tool_diameter, tool)
        if profile is None:
            issues.append(
                SimulationIssue(
                    code="E3001",
                    message=f"{toolpath_pass.id}: no tool diameter available for simulation",
                    operation=toolpath_pass.operation_id,
                )
            )
            continue
        radius = profile.diameter / 2
        for move_index, move in enumerate(toolpath_pass.moves):
            if isinstance(move, RapidMove):
                next_position = _next_position(position, move)
                if _rapid_has_xy_motion(position, next_position) and min(position[2], next_position[2]) < request.machine.machine.clear_z - 1e-9:
                    unsafe_rapid_count += 1
                    issues.append(
                        SimulationIssue(
                            code="W2001",
                            message=f"{toolpath_pass.id}: rapid XY motion occurs below clear_z",
                            operation=toolpath_pass.operation_id,
                            move_index=move_index,
                        )
                    )
                start_depth = _depth_from_z(position[2], request.stock.top_z)
                end_depth = _depth_from_z(next_position[2], request.stock.top_z)
                if grid.would_remove_swept_line(
                    (position[0], position[1]),
                    (next_position[0], next_position[1]),
                    start_depth,
                    end_depth,
                    radius,
                ):
                    rapid_collision_count += 1
                    issues.append(
                        SimulationIssue(
                            code="E3002",
                            message=f"{toolpath_pass.id}: rapid move intersects remaining stock",
                            operation=toolpath_pass.operation_id,
                            move_index=move_index,
                        )
                    )
                position = next_position
            elif isinstance(move, LineMove):
                next_position = _next_position(position, move)
                changed = _remove_segment(grid, position, next_position, radius, toolpath_pass.operation_id, profile)
                if changed == 0 and max(_depth_from_z(position[2], request.stock.top_z), _depth_from_z(next_position[2], request.stock.top_z)) > 0:
                    air_cut_moves += 1
                position = next_position
            elif isinstance(move, ArcMove):
                points = _arc_points(position, move, request.settings.arc_chord_fraction, grid.xy_spacing)
                changed_total = 0
                start = position
                for end in points:
                    changed_total += _remove_segment(grid, start, end, radius, toolpath_pass.operation_id, profile)
                    start = end
                if changed_total == 0 and points:
                    air_cut_moves += 1
                position = points[-1] if points else _next_position(position, move)
            else:
                continue

    response = DexelSimulationResponse(
        errors=[issue for issue in issues if issue.code.startswith("E")],
        warnings=[issue for issue in issues if issue.code.startswith("W")],
        metrics=_metrics(grid, air_cut_moves, rapid_collision_count, unsafe_rapid_count),
        snapshot=grid.snapshot() if include_snapshot else None,
    )
    return DexelSimulationRun(response=response, grid=grid)


def _minimum_tool_diameter(request: DexelSimulationRequest, tools: dict[str, Tool]) -> float:
    diameters = [
        toolpath_pass.tool_diameter
        for toolpath_pass in request.toolpath_plan.passes
        if toolpath_pass.tool_diameter is not None
    ]
    diameters.extend(tool.diameter for tool in tools.values())
    if not diameters:
        raise ValueError("Simulation requires at least one tool diameter")
    return min(diameters)


def _apply_expected_removals(grid: DexelGrid, request: DexelSimulationRequest) -> None:
    for removal in request.expected_removals:
        if removal.type == "circle":
            grid.expected_circle((removal.center_x, removal.center_y), removal.radius, removal.depth)
        elif removal.type == "rectangle":
            grid.expected_rectangle(removal.min_x, removal.min_y, removal.max_x, removal.max_y, removal.depth)


def _tool_profile(pass_diameter: float | None, tool: Tool | None) -> ToolProfile | None:
    diameter = pass_diameter or (tool.diameter if tool is not None else None)
    if diameter is None:
        return None
    return ToolProfile(diameter=diameter, end_type=tool.end_type if tool is not None else "flat")


def _remove_segment(
    grid: DexelGrid,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    operation_id: str,
    profile: ToolProfile,
) -> int:
    return grid.remove_swept_line(
        (start[0], start[1]),
        (end[0], end[1]),
        _depth_from_z(start[2], grid.stock.top_z),
        _depth_from_z(end[2], grid.stock.top_z),
        radius,
        operation_id,
        profile,
    )


def _next_position(position: tuple[float, float, float], move) -> tuple[float, float, float]:
    return (
        float(move.x) if getattr(move, "x", None) is not None else position[0],
        float(move.y) if getattr(move, "y", None) is not None else position[1],
        float(move.z) if getattr(move, "z", None) is not None else position[2],
    )


def _depth_from_z(z: float, stock_top_z: float) -> float:
    return max(0.0, stock_top_z - z)


def _rapid_has_xy_motion(start: tuple[float, float, float], end: tuple[float, float, float]) -> bool:
    return math.hypot(end[0] - start[0], end[1] - start[1]) > 1e-9


def _arc_points(
    position: tuple[float, float, float],
    move: ArcMove,
    arc_chord_fraction: float,
    xy_spacing: float,
) -> list[tuple[float, float, float]]:
    cx = position[0] + move.i
    cy = position[1] + move.j
    radius = math.hypot(position[0] - cx, position[1] - cy)
    if radius <= 1e-12:
        return [_next_position(position, move)]
    start_angle = math.atan2(position[1] - cy, position[0] - cx)
    end_angle = math.atan2(move.y - cy, move.x - cx)
    if move.direction == "ccw":
        sweep = (end_angle - start_angle) % (2 * math.pi)
        if sweep <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = 2 * math.pi
    else:
        sweep = -((start_angle - end_angle) % (2 * math.pi))
        if abs(sweep) <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = -2 * math.pi
    arc_length = abs(sweep) * radius
    chord = max(xy_spacing * arc_chord_fraction, xy_spacing * 0.1)
    steps = max(1, int(math.ceil(arc_length / chord)))
    end_z = move.z if move.z is not None else position[2]
    points = []
    for index in range(1, steps + 1):
        t = index / steps
        angle = start_angle + sweep * t
        points.append(
            (
                cx + math.cos(angle) * radius,
                cy + math.sin(angle) * radius,
                position[2] + (end_z - position[2]) * t,
            )
        )
    points[-1] = (move.x, move.y, end_z)
    return points


def _same_xy(position: tuple[float, float, float], xy: tuple[float, float]) -> bool:
    return math.hypot(position[0] - xy[0], position[1] - xy[1]) <= 1e-9


def _metrics(
    grid: DexelGrid,
    air_cut_moves: int,
    rapid_collision_count: int,
    unsafe_rapid_count: int,
) -> SimulationMetrics:
    tolerance = max(grid.xy_spacing * 0.1, 1e-6)
    overcut = grid.actual_depth > grid.expected_depth + tolerance
    undercut = grid.expected_depth > grid.actual_depth + tolerance
    return SimulationMetrics(
        grid_width=grid.width,
        grid_height=grid.height,
        xy_spacing=float(grid.xy_spacing),
        stock_thickness=float(grid.stock.thickness),
        removed_cells=int(np.count_nonzero(grid.actual_depth > tolerance)),
        expected_removed_cells=int(np.count_nonzero(grid.expected_depth > tolerance)),
        overcut_cells=int(np.count_nonzero(overcut)),
        undercut_cells=int(np.count_nonzero(undercut)),
        recut_cells=int(np.count_nonzero(grid.cut_count > 1)),
        air_cut_moves=air_cut_moves,
        rapid_collision_count=rapid_collision_count,
        unsafe_rapid_count=unsafe_rapid_count,
        max_cut_count=int(grid.cut_count.max(initial=0)),
        max_actual_depth=float(grid.actual_depth.max(initial=0.0)),
        max_expected_depth=float(grid.expected_depth.max(initial=0.0)),
    )
