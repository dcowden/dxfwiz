from __future__ import annotations

import math

from dxfwiz.schemas.job import DrillOperation, HelicalDrillOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import ToolpathPass


def drill_operation_to_toolpaths(
    operation: DrillOperation,
    center: tuple[float, float],
    tool: Tool,
    safe_z: float,
) -> list[ToolpathPass]:
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
            z_top=0.0,
            z_bottom=target,
            moves=moves,
        )
    ]


def helical_drill_operation_to_toolpaths(
    operation: HelicalDrillOperation,
    center: tuple[float, float],
    hole_diameter: float,
    tool: Tool,
    safe_z: float,
    finishing_allowance: float = 0.01,
    max_slug_diameter: float = 0.5,
) -> list[ToolpathPass]:
    target = -operation.depth
    finish_radius = (hole_diameter - tool.diameter) / 2
    rough_radius = finish_radius
    if operation.finishing.enabled and operation.finishing.side:
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
    slug_diameter = max(0.0, 2 * (rough_radius - tool.diameter / 2))
    if slug_diameter > max_slug_diameter:
        warnings.append(
            f"{operation.id}: helical drilling leaves an interior slug about {slug_diameter:.3f} diameter"
        )
    passes = [
        ToolpathPass(
            id=f"{operation.id}-rough-helix",
            operation_id=operation.id,
            entity=operation.entity,
            kind="helical_drill",
            tool=operation.tool,
            tool_diameter=tool.diameter,
            z_top=0.0,
            z_bottom=target,
            moves=_helix_moves(center, rough_radius, target, operation.pitch, direction, safe_z, operation.feed_rate or tool.feed_rate),
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
                    z_top=0.0,
                    z_bottom=target,
                    moves=_finish_circle_moves(center, finish_radius, target, direction, safe_z, operation.feed_rate or tool.feed_rate),
                )
            )
    return passes


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


def _finish_circle_moves(
    center: tuple[float, float],
    radius: float,
    z: float,
    direction: str,
    safe_z: float,
    feed: float,
) -> list[dict]:
    start_x = center[0] + radius
    start_y = center[1]
    return [
        {"type": "rapid", "x": start_x, "y": start_y, "z": safe_z},
        {"type": "line", "z": z, "feed": feed},
        {"type": "arc", "direction": direction, "x": start_x, "y": start_y, "z": z, "i": -radius, "j": 0.0, "feed": feed},
        {"type": "rapid", "z": safe_z},
    ]


def _helical_warning_pass(
    operation: HelicalDrillOperation,
    tool: Tool,
    z_bottom: float,
    warning: str,
    pass_id: str | None = None,
    kind: str = "helical_drill",
) -> ToolpathPass:
    return ToolpathPass(
        id=pass_id or f"{operation.id}-rough-helix",
        operation_id=operation.id,
        entity=operation.entity,
        kind=kind,
        tool=operation.tool,
        tool_diameter=tool.diameter,
        z_top=0.0,
        z_bottom=z_bottom,
        moves=[],
        warnings=[warning],
    )
