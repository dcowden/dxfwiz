from __future__ import annotations

import math

from dxfwiz.schemas.job import DrillOperation, HelicalContourOperation, HelicalPocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import ToolpathPass


def drill_operation_to_toolpaths(
    operation: DrillOperation,
    center: tuple[float, float],
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    effective_feed = operation.feed_rate or tool.feed_rate
    moves: list[dict] = [{"type": "rapid", "x": center[0], "y": center[1], "z": safe_z}]
    target = -operation.depth
    peck = min(operation.peck_depth, operation.depth)
    retract = operation.retract_amount
    feed = operation.plunge_rate or tool.plunge_rate
    depth = 0.0
    while depth > target + 1e-9:
        depth = max(depth - peck, target)
        moves.append({"type": "line", "z": depth, "feed": feed})
        if operation.dwell_time:
            moves.append({"type": "dwell", "seconds": operation.dwell_time})
        if depth > target + 1e-9:
            moves.append({"type": "rapid", "z": min(safe_z, depth + retract)})
    moves.append({"type": "rapid", "z": safe_z})
    return [
        ToolpathPass(
            id=f"{operation.id}-peck",
            operation_id=operation.id,
            entity=operation.entity,
            kind="peck_drill",
            tool=operation.tool,
            tool_diameter=tool.diameter,
            feed_rate=effective_feed,
            z_top=0.0,
            z_bottom=target,
            moves=moves,
        )
    ]


def helical_contour_operation_to_toolpaths(
    operation: HelicalContourOperation,
    center: tuple[float, float],
    hole_diameter: float,
    tool: Tool,
    safe_z: float,
    finishing_allowance: float = 0.01,
    max_slug_diameter: float = 0.5,
) -> list[ToolpathPass]:
    target = -operation.depth
    effective_feed = operation.feed_rate or tool.feed_rate
    finish_radius = (hole_diameter - tool.diameter) / 2
    rough_radius = finish_radius
    skip_roughing_for_fit = False
    if operation.finishing.enabled and operation.finishing.side:
        if finish_radius > finishing_allowance:
            rough_radius -= finishing_allowance
        elif finish_radius > 0 and operation.skip_roughing_when_finish_fits:
            skip_roughing_for_fit = True
        else:
            rough_radius -= finishing_allowance
    direction = "ccw" if operation.milling_direction == "climb" else "cw"
    warnings = []
    if rough_radius <= 0:
        return [
            _helical_warning_pass(
                operation,
                tool,
                target,
                f"{operation.id}: tool diameter {tool.diameter:.6f} is too large for hole diameter {hole_diameter:.6f}",
            )
        ]
    if skip_roughing_for_fit:
        warnings.append(
            f"{operation.id}: skipped roughing pass to accommodate selected tool; "
            f"finish radius {finish_radius:.6f} fits but requested roughing allowance {finishing_allowance:.6f} does not."
        )
        passes = []
    else:
        slug_diameter = max(0.0, 2 * (rough_radius - tool.diameter / 2))
        if slug_diameter > max_slug_diameter:
            warnings.append(
                f"{operation.id}: helical contour leaves an interior slug about {slug_diameter:.3f} diameter"
            )
        passes = [
            ToolpathPass(
                id=f"{operation.id}-rough-helix",
                operation_id=operation.id,
                entity=operation.entity,
                kind="helical_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=effective_feed,
                z_top=0.0,
                z_bottom=target,
                moves=_helix_moves(center, rough_radius, target, operation.pitch, direction, safe_z, effective_feed),
                warnings=warnings,
            )
        ]
    if operation.finishing.enabled and operation.finishing.side:
        if finish_radius <= 0:
            passes.append(
                _helical_warning_pass(
                    operation,
                    tool,
                    target,
                    f"{operation.id}: finish radius is not machinable for hole diameter {hole_diameter:.6f}",
                    pass_id=f"{operation.id}-finish",
                    kind="finish_contour",
                )
            )
        else:
            passes.append(
                ToolpathPass(
                    id=f"{operation.id}-finish",
                    operation_id=operation.id,
                    entity=operation.entity,
                    kind="finish_contour",
                    tool=operation.tool,
                    tool_diameter=tool.diameter,
                    feed_rate=effective_feed,
                    z_top=0.0,
                    z_bottom=target,
                    moves=_finish_circle_moves(center, finish_radius, target, direction, safe_z, effective_feed),
                    warnings=warnings if skip_roughing_for_fit else [],
                )
            )
    return passes


def helical_pocket_operation_to_toolpaths(
    operation: HelicalPocketOperation,
    center: tuple[float, float],
    hole_diameter: float,
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
    target = -operation.depth
    effective_feed = operation.feed_rate or tool.feed_rate
    direction = "ccw" if operation.milling_direction == "climb" else "cw"
    finish_radius = (hole_diameter - tool.diameter) / 2
    rough_radius = finish_radius
    if operation.finishing.enabled and operation.finishing.side:
        rough_radius -= operation.roughing.side_allowance
    warnings: list[str] = []
    if finish_radius <= 0:
        return [
            _helical_warning_pass(
                operation,
                tool,
                target,
                f"{operation.id}: tool diameter {tool.diameter:.6f} is too large for hole diameter {hole_diameter:.6f}",
                kind="helical_pocket",
            )
        ]
    if rough_radius <= 0:
        warnings.append(
            f"{operation.id}: skipped helical pocket roughing because finishing allowance leaves no roughable radius."
        )
        rough_passes: list[ToolpathPass] = []
    else:
        rough_passes = [
            ToolpathPass(
                id=f"{operation.id}-rough-helical-pocket",
                operation_id=operation.id,
                entity=operation.entity,
                kind="helical_pocket",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=effective_feed,
                z_top=0.0,
                z_bottom=target,
                moves=_helical_pocket_moves(
                    center=center,
                    first_radius=min(tool.diameter / 2 * 0.95, rough_radius),
                    final_radius=rough_radius,
                    target_z=target,
                    pitch=operation.pitch,
                    stepover=tool.diameter * operation.stepover_percent / 100,
                    direction=direction,
                    safe_z=safe_z,
                    feed=effective_feed,
                    retract=not (operation.finishing.enabled and operation.finishing.side),
                ),
                warnings=warnings,
            )
        ]
    finish_passes: list[ToolpathPass] = []
    if operation.finishing.enabled and operation.finishing.side:
        finish_passes.append(
            ToolpathPass(
                id=f"{operation.id}-finish",
                operation_id=operation.id,
                entity=operation.entity,
                kind="finish_contour",
                tool=operation.tool,
                tool_diameter=tool.diameter,
                feed_rate=effective_feed,
                z_top=0.0,
                z_bottom=target,
                moves=_finish_circle_moves(
                    center,
                    finish_radius,
                    target,
                    direction,
                    safe_z,
                    effective_feed,
                    enter_at_safe_z=not rough_passes,
                ),
            )
        )
    return [*rough_passes, *finish_passes]


def _helix_moves(
    center: tuple[float, float],
    radius: float,
    target_z: float,
    pitch: float,
    direction: str,
    safe_z: float,
    feed: float,
) -> list[dict]:
    angle = 0.0
    x = center[0] + radius
    y = center[1]
    moves: list[dict] = [
        {"type": "rapid", "x": x, "y": y, "z": safe_z},
        {"type": "line", "z": 0.0, "feed": feed},
    ]
    z = 0.0
    while z > target_z + 1e-9:
        remaining = z - target_z
        step = min(pitch, remaining)
        fraction = step / pitch
        sweep = (2 * math.pi * fraction) * (1 if direction == "ccw" else -1)
        angle += sweep
        next_x = center[0] + math.cos(angle) * radius
        next_y = center[1] + math.sin(angle) * radius
        next_z = z - step
        moves.append(
            {
                "type": "arc",
                "direction": direction,
                "x": next_x,
                "y": next_y,
                "z": next_z,
                "i": center[0] - x,
                "j": center[1] - y,
                "feed": feed,
            }
        )
        x, y, z = next_x, next_y, next_z
    moves.append({"type": "rapid", "z": safe_z})
    return moves


def _helical_pocket_moves(
    center: tuple[float, float],
    first_radius: float,
    final_radius: float,
    target_z: float,
    pitch: float,
    stepover: float,
    direction: str,
    safe_z: float,
    feed: float,
    retract: bool = True,
) -> list[dict]:
    radius = max(1e-6, first_radius)
    moves = _helix_moves(center, radius, target_z, pitch, direction, safe_z, feed)
    if moves and moves[-1].get("type") == "rapid":
        moves.pop()
    current_xy = _current_xy_from_moves(moves, center, radius)
    start_angle = math.atan2(current_xy[1] - center[1], current_xy[0] - center[0])
    spiral_moves = _spiral_out_arc_moves(
        center=center,
        start_radius=radius,
        final_radius=final_radius,
        z=target_z,
        stepover=stepover,
        direction=direction,
        feed=feed,
        start_angle=start_angle,
    )
    moves.extend(spiral_moves)
    current_xy = _current_xy_from_moves(moves, center, final_radius)
    moves.append(_full_circle_arc_from_start(center, current_xy, target_z, direction, feed))
    if retract:
        moves.append({"type": "rapid", "z": safe_z})
    return moves


def _spiral_out_arc_moves(
    center: tuple[float, float],
    start_radius: float,
    final_radius: float,
    z: float,
    stepover: float,
    direction: str,
    feed: float,
    start_angle: float = 0.0,
) -> list[dict]:
    if final_radius <= start_radius + 1e-9:
        return []
    radial_growth_per_radian = max(stepover, 1e-6) / (2 * math.pi)
    total_angle = (final_radius - start_radius) / radial_growth_per_radian
    max_sweep = math.pi / 2
    segment_count = max(1, math.ceil(total_angle / max_sweep))
    signed_total = total_angle * (1 if direction == "ccw" else -1)
    moves = []
    previous = _spiral_point(center, start_radius, start_angle)
    for index in range(1, segment_count + 1):
        t = index / segment_count
        angle = start_angle + signed_total * t
        radius = final_radius if index == segment_count else start_radius + (final_radius - start_radius) * t
        end = _spiral_point(center, radius, angle)
        move = _spiral_arc_segment(
            center=center,
            start=previous,
            end=end,
            start_radius=start_radius + (final_radius - start_radius) * ((index - 1) / segment_count),
            angle=start_angle + signed_total * ((index - 1) / segment_count),
            radial_growth_per_radian=radial_growth_per_radian,
            direction=direction,
            z=z,
            feed=feed,
        )
        moves.append(move)
        previous = end
    return moves


def _spiral_point(center: tuple[float, float], radius: float, angle: float) -> tuple[float, float]:
    return center[0] + math.cos(angle) * radius, center[1] + math.sin(angle) * radius


def _spiral_arc_segment(
    center: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    start_radius: float,
    angle: float,
    radial_growth_per_radian: float,
    direction: str,
    z: float,
    feed: float,
) -> dict:
    sign = 1 if direction == "ccw" else -1
    dx_dtheta = radial_growth_per_radian * math.cos(angle) - start_radius * math.sin(angle) * sign
    dy_dtheta = radial_growth_per_radian * math.sin(angle) + start_radius * math.cos(angle) * sign
    tangent_len = math.hypot(dx_dtheta, dy_dtheta)
    if tangent_len <= 1e-12:
        normal = (-math.sin(angle), math.cos(angle))
    else:
        tangent = (dx_dtheta / tangent_len, dy_dtheta / tangent_len)
        normal = (-tangent[1], tangent[0]) if direction == "ccw" else (tangent[1], -tangent[0])
    chord = (end[0] - start[0], end[1] - start[1])
    denom = 2 * (normal[0] * chord[0] + normal[1] * chord[1])
    if abs(denom) <= 1e-12:
        # Extremely tiny spiral segments are effectively circular around the hole center.
        arc_center = center
    else:
        circle_radius = (chord[0] ** 2 + chord[1] ** 2) / denom
        arc_center = (start[0] + normal[0] * circle_radius, start[1] + normal[1] * circle_radius)
    return {
        "type": "arc",
        "direction": direction,
        "x": end[0],
        "y": end[1],
        "z": z,
        "i": arc_center[0] - start[0],
        "j": arc_center[1] - start[1],
        "feed": feed,
    }


def _full_circle_arc(
    center: tuple[float, float],
    radius: float,
    z: float,
    direction: str,
    feed: float,
) -> dict:
    return {
        "type": "arc",
        "direction": direction,
        "x": center[0] + radius,
        "y": center[1],
        "z": z,
        "i": -radius,
        "j": 0.0,
        "feed": feed,
    }


def _full_circle_arc_from_start(
    center: tuple[float, float],
    start: tuple[float, float],
    z: float,
    direction: str,
    feed: float,
) -> dict:
    return {
        "type": "arc",
        "direction": direction,
        "x": start[0],
        "y": start[1],
        "z": z,
        "i": center[0] - start[0],
        "j": center[1] - start[1],
        "feed": feed,
    }


def _current_xy_from_moves(moves: list[dict], center: tuple[float, float], radius: float) -> tuple[float, float]:
    for move in reversed(moves):
        if move.get("x") is not None and move.get("y") is not None:
            return float(move["x"]), float(move["y"])
    return center[0] + radius, center[1]


def _finish_circle_moves(
    center: tuple[float, float],
    radius: float,
    z: float,
    direction: str,
    safe_z: float,
    feed: float,
    enter_at_safe_z: bool = True,
) -> list[dict]:
    start_x = center[0] + radius
    start_y = center[1]
    entry_moves = (
        [
            {"type": "rapid", "x": start_x, "y": start_y, "z": safe_z},
            {"type": "line", "z": z, "feed": feed},
        ]
        if enter_at_safe_z
        else [{"type": "line", "x": start_x, "y": start_y, "z": z, "feed": feed}]
    )
    return [
        *entry_moves,
        {"type": "arc", "direction": direction, "x": start_x, "y": start_y, "z": z, "i": -radius, "j": 0.0, "feed": feed},
        {"type": "rapid", "z": safe_z},
    ]


def _helical_warning_pass(
    operation: HelicalContourOperation | HelicalPocketOperation,
    tool: Tool,
    z_bottom: float,
    warning: str,
    pass_id: str | None = None,
    kind: str = "helical_contour",
) -> ToolpathPass:
    return ToolpathPass(
        id=pass_id or f"{operation.id}-rough-helix",
        operation_id=operation.id,
        entity=operation.entity,
        kind=kind,
        tool=operation.tool,
        tool_diameter=tool.diameter,
        feed_rate=operation.feed_rate or tool.feed_rate,
        z_top=0.0,
        z_bottom=z_bottom,
        moves=[],
        warnings=[warning],
    )
