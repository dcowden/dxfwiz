from __future__ import annotations

from dxfwiz.schemas.job import MoveOperation
from dxfwiz.toolpaths.model import ToolpathPass


def move_operation_to_toolpaths(operation: MoveOperation) -> list[ToolpathPass]:
    move_type = "rapid" if operation.is_rapid else "line"
    move: dict = {
        "type": move_type,
        "x": operation.x,
        "y": operation.y,
        "z": operation.z,
    }
    if not operation.is_rapid and operation.feed_rate is not None:
        move["feed"] = operation.feed_rate
    return [
        ToolpathPass(
            id=f"{operation.id}-move",
            operation_id=operation.id,
            entity=None,
            kind="move",
            tool=None,
            tool_diameter=None,
            feed_rate=operation.feed_rate,
            z_top=operation.z or 0.0,
            z_bottom=operation.z or 0.0,
            moves=[move],
        )
    ]
