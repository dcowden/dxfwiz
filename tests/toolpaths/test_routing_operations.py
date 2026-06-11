from pathlib import Path
import random

import pytest

from dxfwiz.schemas.job import DrillOperation, OperationGroup
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.drilling import drill_operation_to_toolpaths
from dxfwiz.toolpaths.model import SourcePath
from dxfwiz.toolpaths.operations import render_toolpath_preview_sheet_svg
from dxfwiz.toolpaths.routing import route_passes_within_groups


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
PREVIEWS = []


def teardown_module():
    if PREVIEWS:
        render_toolpath_preview_sheet_svg(PREVIEWS, OUTPUT_DIR / "test_routing_operations.svg")


def test_route_drill_operations_within_group_by_nearest_neighbor():
    random.seed(31337)
    centers = [(round(random.uniform(0.5, 9.5), 3), round(random.uniform(0.5, 5.5), 3)) for _ in range(10)]
    deliberately_bad_order = [0, 9, 1, 8, 2, 7, 3, 6, 4, 5]
    tool = make_test_tool()
    passes = []
    for index in deliberately_bad_order:
        operation = DrillOperation.model_validate(
            {
                "id": f"op-hole-{index}",
                "type": "drill",
                "entity": f"e-hole-{index}",
                "tool": "t5",
                "depth": 0.2,
                "peck_depth": 0.1,
                "retract_amount": 0.04,
            }
        )
        passes.extend(drill_operation_to_toolpaths(operation, centers[index], tool, safe_z=0.5))
    group = OperationGroup(name="holes", operations=[toolpath_pass.operation_id for toolpath_pass in passes])

    routed = route_passes_within_groups(passes, [group], safe_z=0.5, start_xy=(0.0, 0.0))

    original_order = [toolpath_pass.operation_id for toolpath_pass in passes]
    routed_order = [toolpath_pass.operation_id for toolpath_pass in routed]
    center_by_operation = {f"op-hole-{index}": centers[index] for index in range(len(centers))}
    assert routed_order != original_order
    assert set(routed_order) == set(original_order)
    assert _order_travel_length(routed_order, center_by_operation) < _order_travel_length(original_order, center_by_operation)
    assert all(_last_z(toolpath_pass) == pytest.approx(0.5) for toolpath_pass in routed)
    assert any(_has_xy_rapid_link(toolpath_pass) for toolpath_pass in routed)
    PREVIEWS.append(("test_route_drill_operations_within_group_by_nearest_neighbor", drill_preview_source_path(centers), routed))


def test_route_prioritizes_tool_changes_before_group_order_when_configured():
    centers = {
        "op-fixture-small": (0.0, 0.0),
        "op-fixture-large": (10.0, 0.0),
        "op-contour-small": (1.0, 0.0),
        "op-contour-large": (11.0, 0.0),
    }
    passes = [
        *_drill_pass("op-fixture-small", "t1", centers["op-fixture-small"]),
        *_drill_pass("op-fixture-large", "t5", centers["op-fixture-large"]),
        *_drill_pass("op-contour-small", "t1", centers["op-contour-small"]),
        *_drill_pass("op-contour-large", "t5", centers["op-contour-large"]),
    ]
    groups = [
        OperationGroup(name="fixtures", operations=["op-fixture-small", "op-fixture-large"]),
        OperationGroup(name="contours", operations=["op-contour-small", "op-contour-large"]),
    ]

    routed = route_passes_within_groups(
        passes,
        groups,
        safe_z=0.5,
        start_xy=(0.0, 0.0),
        operation_sort=["tool", "group", "nest_order"],
    )

    assert [toolpath_pass.operation_id for toolpath_pass in routed] == [
        "op-fixture-small",
        "op-contour-small",
        "op-fixture-large",
        "op-contour-large",
    ]


def test_route_can_prioritize_group_order_before_tool_changes_when_configured():
    centers = {
        "op-fixture-small": (0.0, 0.0),
        "op-fixture-large": (10.0, 0.0),
        "op-contour-small": (1.0, 0.0),
        "op-contour-large": (11.0, 0.0),
    }
    passes = [
        *_drill_pass("op-fixture-small", "t1", centers["op-fixture-small"]),
        *_drill_pass("op-fixture-large", "t5", centers["op-fixture-large"]),
        *_drill_pass("op-contour-small", "t1", centers["op-contour-small"]),
        *_drill_pass("op-contour-large", "t5", centers["op-contour-large"]),
    ]
    groups = [
        OperationGroup(name="fixtures", operations=["op-fixture-small", "op-fixture-large"]),
        OperationGroup(name="contours", operations=["op-contour-small", "op-contour-large"]),
    ]

    routed = route_passes_within_groups(
        passes,
        groups,
        safe_z=0.5,
        start_xy=(0.0, 0.0),
        operation_sort=["group", "tool", "nest_order"],
    )

    assert [toolpath_pass.operation_id for toolpath_pass in routed] == [
        "op-fixture-small",
        "op-fixture-large",
        "op-contour-small",
        "op-contour-large",
    ]


def _order_travel_length(
    order: list[str],
    center_by_operation: dict[str, tuple[float, float]],
    start_xy: tuple[float, float] = (0.0, 0.0),
) -> float:
    current_xy = start_xy
    distance = 0.0
    for operation_id in order:
        next_xy = center_by_operation[operation_id]
        distance += _distance(current_xy, next_xy)
        current_xy = next_xy
    return distance


def _has_xy_rapid_link(toolpath_pass) -> bool:
    rapid_moves = [move for move in toolpath_pass.moves if move.type == "rapid" and move.x is not None and move.y is not None]
    return len(rapid_moves) >= 2


def _last_z(toolpath_pass) -> float | None:
    z = None
    for move in toolpath_pass.moves:
        if move.z is not None:
            z = move.z
    return z


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return ((first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2) ** 0.5


def drill_preview_source_path(centers: list[tuple[float, float]]) -> SourcePath:
    min_x = min(x for x, _y in centers) - 0.5
    max_x = max(x for x, _y in centers) + 0.5
    min_y = min(y for _x, y in centers) - 0.5
    max_y = max(y for _x, y in centers) + 0.5
    from dxfwiz.schemas.common import Point2D
    from dxfwiz.toolpaths.model import SourceLineSegment

    points = [
        Point2D(x=min_x, y=min_y),
        Point2D(x=max_x, y=min_y),
        Point2D(x=max_x, y=max_y),
        Point2D(x=min_x, y=max_y),
    ]
    return SourcePath(
        id="routing-preview-bounds",
        entity="routing-preview",
        closed=True,
        segments=[
            SourceLineSegment(type="line", start=start, end=end)
            for start, end in zip(points, [*points[1:], points[0]], strict=True)
        ],
    )


def make_test_tool() -> Tool:
    return make_tool("t5")


def _drill_pass(operation_id: str, tool_id: str, center: tuple[float, float]):
    operation = DrillOperation.model_validate(
        {
            "id": operation_id,
            "type": "drill",
            "entity": f"e-{operation_id}",
            "tool": tool_id,
            "depth": 0.2,
            "peck_depth": 0.1,
            "retract_amount": 0.04,
        }
    )
    return drill_operation_to_toolpaths(operation, center, make_tool(tool_id), safe_z=0.5)


def make_tool(tool_id: str) -> Tool:
    return Tool.model_validate(
        {
            "id": tool_id,
            "description": "test tool",
            "end_type": "flat",
            "flute_spiral": "upcut",
            "diameter": 0.125,
            "flutes": 2,
            "speed": 18000,
            "feed_rate": 60,
            "plunge_rate": 20,
            "depth_per_pass": 0.1,
        }
    )
