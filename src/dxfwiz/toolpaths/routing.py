from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from dxfwiz.schemas.job import OperationGroup
from dxfwiz.toolpaths.model import RapidMove, ToolpathMove, ToolpathPass


def route_passes_within_groups(
    passes: Sequence[ToolpathPass],
    operation_groups: Sequence[OperationGroup],
    safe_z: float,
    start_xy: tuple[float, float] | None = None,
    operation_sort: Sequence[str] | None = None,
    role_by_operation: dict[str, str] | None = None,
    nest_order_by_operation: dict[str, int] | None = None,
) -> list[ToolpathPass]:
    """Order operations by configured CAM priorities, then greedily by travel.

    This is intentionally a heuristic, not exact TSP. It mirrors the practical
    nearest-neighbor ordering used by CAM emitters such as Kiri:Moto's
    poly2polyEmit/tip2tipEmit helpers.
    """
    sort_priorities = list(operation_sort or ["tool", "group", "nest_order"])
    pass_groups = _passes_by_operation(passes)
    group_index_by_operation = {
        operation_id: group_index
        for group_index, group in enumerate(operation_groups)
        for operation_id in group.operations
    }
    group_name_by_operation = {
        operation_id: group.name
        for group in operation_groups
        for operation_id in group.operations
    }
    operation_ids = _ordered_operation_ids(passes, operation_groups)
    operations = [
        _OperationPasses(
            operation_id=operation_id,
            passes=pass_groups[operation_id],
            group_index=group_index_by_operation.get(operation_id, len(operation_groups)),
            group_name=group_name_by_operation.get(operation_id, "ungrouped"),
            role=(role_by_operation or {}).get(operation_id),
            nest_order=(nest_order_by_operation or {}).get(operation_id),
        )
        for operation_id in operation_ids
        if operation_id in pass_groups
    ]
    routed: list[ToolpathPass] = []
    current_xy = start_xy
    for _bucket_key, candidates in _operation_buckets(operations, sort_priorities):
        ordered = _nearest_neighbor_order(candidates, current_xy)
        for candidate in ordered:
            safe_passes = [_ensure_safe_z_exit(toolpath_pass, safe_z) for toolpath_pass in candidate.passes]
            safe_passes = _prepend_operation_link(safe_passes, current_xy, safe_z)
            routed.extend(safe_passes)
            current_xy = _operation_end_xy(safe_passes) or current_xy

    return routed


class _OperationPasses:
    def __init__(
        self,
        operation_id: str,
        passes: list[ToolpathPass],
        group_index: int,
        group_name: str,
        role: str | None,
        nest_order: int | None,
    ) -> None:
        self.operation_id = operation_id
        self.passes = passes
        self.group_index = group_index
        self.group_name = group_name
        self.role = role
        self.nest_order = nest_order

    @property
    def start_xy(self) -> tuple[float, float] | None:
        return _operation_start_xy(self.passes)

    @property
    def end_xy(self) -> tuple[float, float] | None:
        return _operation_end_xy(self.passes)

    @property
    def tool(self) -> str:
        return next((toolpath_pass.tool for toolpath_pass in self.passes if toolpath_pass.tool), "")


def _passes_by_operation(passes: Sequence[ToolpathPass]) -> dict[str, list[ToolpathPass]]:
    grouped: dict[str, list[ToolpathPass]] = {}
    for toolpath_pass in passes:
        grouped.setdefault(toolpath_pass.operation_id, []).append(toolpath_pass)
    return grouped


def _ordered_operation_ids(passes: Sequence[ToolpathPass], operation_groups: Sequence[OperationGroup]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for group in operation_groups:
        for operation_id in group.operations:
            if operation_id not in seen:
                ids.append(operation_id)
                seen.add(operation_id)
    for toolpath_pass in passes:
        if toolpath_pass.operation_id not in seen:
            ids.append(toolpath_pass.operation_id)
            seen.add(toolpath_pass.operation_id)
    return ids


def _operation_buckets(
    operations: list[_OperationPasses],
    priorities: Sequence[str],
) -> list[tuple[tuple, list[_OperationPasses]]]:
    buckets: dict[tuple, list[_OperationPasses]] = defaultdict(list)
    for operation in operations:
        buckets[_sort_key(operation, priorities)].append(operation)
    return sorted(buckets.items(), key=lambda item: item[0])


def _sort_key(operation: _OperationPasses, priorities: Sequence[str]) -> tuple:
    return tuple(_priority_value(operation, priority) for priority in priorities)


def _priority_value(operation: _OperationPasses, priority: str):
    if priority == "tool":
        return operation.tool
    if priority == "group":
        return operation.group_index
    if priority == "role":
        return operation.role or _role_from_passes(operation.passes)
    if priority == "nest_order":
        return operation.nest_order if operation.nest_order is not None else _nest_order_from_passes(operation.passes)
    return 0


def _role_from_passes(passes: Sequence[ToolpathPass]) -> str:
    kind = next((toolpath_pass.kind for toolpath_pass in passes), "")
    if kind == "peck_drill":
        return "hole"
    if kind == "helical_drill":
        return "hole"
    if kind.startswith("pocket"):
        return "pocket"
    if kind.endswith("contour"):
        return "contour"
    return kind


def _nest_order_from_passes(passes: Sequence[ToolpathPass]) -> int:
    role = _role_from_passes(passes)
    if role == "hole":
        return 0
    if role == "pocket":
        return 1
    if role == "contour":
        return 2
    return 1


def _nearest_neighbor_order(
    candidates: list[_OperationPasses],
    start_xy: tuple[float, float] | None,
) -> list[_OperationPasses]:
    remaining = candidates[:]
    ordered: list[_OperationPasses] = []
    current_xy = start_xy
    while remaining:
        if current_xy is None:
            next_candidate = remaining.pop(0)
        else:
            next_index = min(
                range(len(remaining)),
                key=lambda index: _distance(current_xy, remaining[index].start_xy),
            )
            next_candidate = remaining.pop(next_index)
        ordered.append(next_candidate)
        current_xy = next_candidate.end_xy or current_xy
    return ordered


def _ensure_safe_z_exit(toolpath_pass: ToolpathPass, safe_z: float) -> ToolpathPass:
    if _ends_at_safe_z(toolpath_pass.moves, safe_z):
        return toolpath_pass
    return toolpath_pass.model_copy(update={"moves": [*toolpath_pass.moves, RapidMove(type="rapid", z=safe_z)]})


def _prepend_operation_link(
    passes: list[ToolpathPass],
    from_xy: tuple[float, float] | None,
    safe_z: float,
) -> list[ToolpathPass]:
    if from_xy is None or not passes:
        return passes
    return [_prepend_rapid_link(passes[0], from_xy, safe_z), *passes[1:]]


def _prepend_rapid_link(
    toolpath_pass: ToolpathPass,
    from_xy: tuple[float, float],
    safe_z: float,
) -> ToolpathPass:
    start_xy = _pass_start_xy(toolpath_pass)
    if start_xy is None or _distance(from_xy, start_xy) <= 1e-9:
        return toolpath_pass
    link_moves = [
        RapidMove(type="rapid", x=from_xy[0], y=from_xy[1], z=safe_z),
        RapidMove(type="rapid", x=start_xy[0], y=start_xy[1], z=safe_z),
    ]
    return toolpath_pass.model_copy(update={"moves": [*link_moves, *toolpath_pass.moves]})


def _ends_at_safe_z(moves: Sequence[ToolpathMove], safe_z: float) -> bool:
    current_z: float | None = None
    for move in moves:
        z = getattr(move, "z", None)
        if z is not None:
            current_z = z
    return current_z is not None and abs(current_z - safe_z) <= 1e-9


def _operation_start_xy(passes: Sequence[ToolpathPass]) -> tuple[float, float] | None:
    for toolpath_pass in passes:
        start_xy = _pass_start_xy(toolpath_pass)
        if start_xy is not None:
            return start_xy
    return None


def _operation_end_xy(passes: Sequence[ToolpathPass]) -> tuple[float, float] | None:
    for toolpath_pass in reversed(passes):
        end_xy = _pass_end_xy(toolpath_pass)
        if end_xy is not None:
            return end_xy
    return None


def _pass_start_xy(toolpath_pass: ToolpathPass) -> tuple[float, float] | None:
    current_x: float | None = None
    current_y: float | None = None
    for move in toolpath_pass.moves:
        x = getattr(move, "x", None)
        y = getattr(move, "y", None)
        if x is not None:
            current_x = x
        if y is not None:
            current_y = y
        if current_x is not None and current_y is not None:
            return current_x, current_y
    return None


def _pass_end_xy(toolpath_pass: ToolpathPass) -> tuple[float, float] | None:
    current_x: float | None = None
    current_y: float | None = None
    for move in toolpath_pass.moves:
        x = getattr(move, "x", None)
        y = getattr(move, "y", None)
        if x is not None:
            current_x = x
        if y is not None:
            current_y = y
    if current_x is None or current_y is None:
        return None
    return current_x, current_y


def _distance(first: tuple[float, float], second: tuple[float, float] | None) -> float:
    if second is None:
        return math.inf
    return math.hypot(first[0] - second[0], first[1] - second[1])
