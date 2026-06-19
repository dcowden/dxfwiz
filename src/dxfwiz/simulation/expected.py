from __future__ import annotations

import math
from dataclasses import dataclass

from dxfwiz.schemas import GeometryFile
from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import (
    ContourOperation,
    DrillOperation,
    HelicalContourOperation,
    HelicalPocketOperation,
    JobFile,
    MoveOperation,
    PocketOperation,
    TraceOperation,
)
from dxfwiz.simulation.model import ExpectedRemoval
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove, SourcePath, ToolpathPass, ToolpathPlan
from dxfwiz.toolpaths.operations import source_path_points


@dataclass(frozen=True)
class ExpectedRemovalBuildResult:
    removals: list[ExpectedRemoval]
    supported_operation_ids: set[str]
    warnings: list[str]


def build_expected_removals(
    job: JobFile,
    geometry: GeometryFile,
    toolpath_plan: ToolpathPlan | None = None,
    xy_spacing: float | None = None,
    arc_chord_fraction: float = 1.0,
) -> ExpectedRemovalBuildResult:
    entities = {entity.id: entity for entity in geometry.entities}
    generated = {entity.id: entity for entity in job.generated_entities}
    source_paths_by_entity = _source_paths_by_entity(toolpath_plan)
    removals: list[ExpectedRemoval] = []
    supported_operation_ids: set[str] = set()
    warnings: list[str] = []

    for operation in job.operations:
        if isinstance(operation, DrillOperation | HelicalContourOperation | HelicalPocketOperation):
            entity = entities.get(operation.entity) or generated.get(operation.entity)
            center = getattr(entity, "center", None) if entity is not None else None
            diameter = _operation_hole_diameter(operation, entity)
            if center is None or diameter is None:
                warnings.append(f"{operation.id}: could not build circular expected removal for {operation.entity}")
                continue
            removals.append(
                {
                    "type": "circle",
                    "operation_id": operation.id,
                    "entity": operation.entity,
                    "center_x": center.x,
                    "center_y": center.y,
                    "radius": diameter / 2,
                    "depth": _drill_expected_depth(job, operation, entity),
                }
            )
            supported_operation_ids.add(operation.id)
        elif isinstance(operation, PocketOperation):
            removal = _pocket_expected_removal(operation, entities, source_paths_by_entity)
            if removal is None:
                warnings.append(f"{operation.id}: could not build pocket expected removal for {operation.entity}")
                continue
            removals.append(removal)
            supported_operation_ids.add(operation.id)
        elif isinstance(operation, ContourOperation | TraceOperation):
            swept = _swept_line_removals_for_operation(toolpath_plan, operation.id, xy_spacing, arc_chord_fraction)
            if not swept:
                warnings.append(f"{operation.id}: could not build swept expected removal for {operation.type}")
                continue
            removals.extend(swept)
            supported_operation_ids.add(operation.id)
        elif isinstance(operation, MoveOperation):
            supported_operation_ids.add(operation.id)

    return ExpectedRemovalBuildResult(
        removals=removals,
        supported_operation_ids=supported_operation_ids,
        warnings=warnings,
    )


def _operation_hole_diameter(operation: DrillOperation | HelicalContourOperation | HelicalPocketOperation, entity) -> float | None:
    if isinstance(operation, HelicalContourOperation | HelicalPocketOperation) and operation.hole_diameter is not None:
        return operation.hole_diameter
    return getattr(entity, "diameter", None)


def _drill_expected_depth(job: JobFile, operation: DrillOperation | HelicalContourOperation | HelicalPocketOperation, entity) -> float:
    if isinstance(operation, DrillOperation) and getattr(entity, "role", None) == "screw_hole":
        return max(operation.depth, job.stock.thickness)
    return operation.depth


def _pocket_expected_removal(
    operation: PocketOperation,
    entities: dict,
    source_paths_by_entity: dict[str, SourcePath],
) -> ExpectedRemoval | None:
    source_path = source_paths_by_entity.get(operation.entity)
    if source_path is not None and source_path.closed:
        points = source_path_points(source_path)
        if len(points) >= 3:
            return {
                "type": "polygon",
                "operation_id": operation.id,
                "entity": operation.entity,
                "points": [Point2D(x=x, y=y) for x, y in points],
                "depth": operation.depth,
            }

    entity = entities.get(operation.entity)
    if entity is None:
        return None
    if entity.shape == "circle" and entity.center is not None and entity.diameter is not None:
        return {
            "type": "circle",
            "operation_id": operation.id,
            "entity": operation.entity,
            "center_x": entity.center.x,
            "center_y": entity.center.y,
            "radius": entity.diameter / 2,
            "depth": operation.depth,
        }
    if entity.shape == "rectangle" and entity.bounding_box is not None:
        box = entity.bounding_box
        return {
            "type": "rectangle",
            "operation_id": operation.id,
            "entity": operation.entity,
            "min_x": box.min.x,
            "min_y": box.min.y,
            "max_x": box.max.x,
            "max_y": box.max.y,
            "depth": operation.depth,
        }
    return None


def _source_paths_by_entity(toolpath_plan: ToolpathPlan | None) -> dict[str, SourcePath]:
    if toolpath_plan is None:
        return {}
    return {source_path.entity: source_path for source_path in toolpath_plan.source_paths}


def _swept_line_removals_for_operation(
    toolpath_plan: ToolpathPlan | None,
    operation_id: str,
    xy_spacing: float | None = None,
    arc_chord_fraction: float = 1.0,
) -> list[ExpectedRemoval]:
    if toolpath_plan is None:
        return []
    removals: list[ExpectedRemoval] = []
    position = (0.0, 0.0, 0.0)
    for toolpath_pass in toolpath_plan.passes:
        if toolpath_pass.operation_id != operation_id:
            continue
        radius = (toolpath_pass.tool_diameter or 0.0) / 2
        if radius <= 0:
            continue
        position = _append_pass_sweeps(toolpath_pass, position, radius, removals, xy_spacing, arc_chord_fraction)
    return removals


def _append_pass_sweeps(
    toolpath_pass: ToolpathPass,
    position: tuple[float, float, float],
    radius: float,
    removals: list[ExpectedRemoval],
    xy_spacing: float | None = None,
    arc_chord_fraction: float = 1.0,
) -> tuple[float, float, float]:
    for move in toolpath_pass.moves:
        if isinstance(move, RapidMove):
            position = _next_position(position, move)
        elif isinstance(move, LineMove):
            next_position = _next_position(position, move)
            _append_sweep(toolpath_pass, position, next_position, radius, removals)
            position = next_position
        elif isinstance(move, ArcMove):
            points = _arc_points(position, move, xy_spacing, arc_chord_fraction)
            start = position
            for end in points:
                _append_sweep(toolpath_pass, start, end, radius, removals)
                start = end
            position = points[-1] if points else _next_position(position, move)
    return position


def _append_sweep(
    toolpath_pass: ToolpathPass,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    removals: list[ExpectedRemoval],
) -> None:
    start_depth = _depth_from_z(start[2])
    end_depth = _depth_from_z(end[2])
    if max(start_depth, end_depth) <= 1e-9:
        return
    removals.append(
        {
            "type": "swept_line",
            "operation_id": toolpath_pass.operation_id,
            "entity": toolpath_pass.entity,
            "start_x": start[0],
            "start_y": start[1],
            "end_x": end[0],
            "end_y": end[1],
            "start_depth": start_depth,
            "end_depth": end_depth,
            "radius": radius,
        }
    )


def _next_position(position: tuple[float, float, float], move) -> tuple[float, float, float]:
    return (
        float(move.x) if getattr(move, "x", None) is not None else position[0],
        float(move.y) if getattr(move, "y", None) is not None else position[1],
        float(move.z) if getattr(move, "z", None) is not None else position[2],
    )


def _depth_from_z(z: float) -> float:
    return max(0.0, -z)


def _arc_points(
    position: tuple[float, float, float],
    move: ArcMove,
    xy_spacing: float | None = None,
    arc_chord_fraction: float = 1.0,
) -> list[tuple[float, float, float]]:
    center_x = position[0] + move.i
    center_y = position[1] + move.j
    radius = math.hypot(position[0] - center_x, position[1] - center_y)
    if radius <= 1e-12:
        return [_next_position(position, move)]
    start_angle = math.atan2(position[1] - center_y, position[0] - center_x)
    end_angle = math.atan2(move.y - center_y, move.x - center_x)
    if move.direction == "ccw":
        sweep = (end_angle - start_angle) % (2 * math.pi)
        if sweep <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = 2 * math.pi
    else:
        sweep = -((start_angle - end_angle) % (2 * math.pi))
        if abs(sweep) <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = -2 * math.pi
    arc_length = abs(sweep) * radius
    if xy_spacing is None:
        chord = max(radius / 8, 1e-6)
        steps = max(8, int(math.ceil(arc_length / chord)))
    else:
        chord = max(xy_spacing * arc_chord_fraction, xy_spacing * 0.1)
        steps = max(1, int(math.ceil(arc_length / chord)))
    end_z = move.z if move.z is not None else position[2]
    points = []
    for index in range(1, steps + 1):
        fraction = index / steps
        angle = start_angle + sweep * fraction
        points.append(
            (
                center_x + math.cos(angle) * radius,
                center_y + math.sin(angle) * radius,
                position[2] + (end_z - position[2]) * fraction,
            )
        )
    points[-1] = (move.x, move.y, end_z)
    return points


def _same_xy(position: tuple[float, float, float], xy: tuple[float, float]) -> bool:
    return math.hypot(position[0] - xy[0], position[1] - xy[1]) <= 1e-9
