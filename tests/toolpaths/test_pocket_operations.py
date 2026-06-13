from pathlib import Path

import pytest

from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import PocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import SourceLineSegment, SourcePath
from dxfwiz.toolpaths.operations import render_toolpath_preview_sheet_svg
from dxfwiz.toolpaths.pocketing import pocket_operation_to_toolpaths


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
PREVIEWS = []


def teardown_module():
    if PREVIEWS:
        render_toolpath_preview_sheet_svg(PREVIEWS, OUTPUT_DIR / "test_pocket_operations.svg")


def test_raster_pocket_generates_clearing_floor_and_wall_finish():
    source_path = rectangle_source_path("p1", width=3, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = make_pocket_operation(
        strategy="raster",
        roughing={"side_allowance": 0.02, "bottom_allowance": 0.03},
        finishing={"enabled": True, "side": True, "bottom": True, "passes": 1, "milling_direction": "climb"},
    )

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == [
        "pocket_clear",
        "pocket_floor_finish",
        "pocket_wall_finish",
    ]
    assert passes[0].z_bottom == pytest.approx(-0.22)
    assert passes[1].z_bottom == pytest.approx(-0.25)
    assert passes[2].z_bottom == pytest.approx(-0.25)
    assert passes[0].offset_distance == pytest.approx(0.145)
    assert passes[1].offset_distance == pytest.approx(0.125)
    assert len(_xy_line_moves(passes[0])) > len(_xy_line_moves(passes[2]))
    PREVIEWS.append(("test_raster_pocket_generates_clearing_floor_and_wall_finish", source_path, passes))


def test_offset_pocket_generates_nested_offset_loops():
    source_path = rectangle_source_path("p2", width=4, height=3)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="offset", stepover_percent=50)

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert passes[0].kind == "pocket_clear"
    rapid_starts = [move for move in passes[0].moves if move.type == "rapid" and move.x is not None and move.y is not None]
    assert len(rapid_starts) == 2
    assert len(_xy_line_moves(passes[0])) > 20
    assert not passes[0].warnings
    PREVIEWS.append(("test_offset_pocket_generates_nested_offset_loops", source_path, passes))


def test_adaptive_pocket_generates_ordered_closed_offset_loops():
    source_path = l_shape_source_path("p-adaptive")
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="adaptive", stepover_percent=40)

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert passes[0].kind == "pocket_clear"
    assert passes[0].offset_distance == pytest.approx(0.125)
    assert not passes[0].warnings
    rough_segments = _xy_segments_at_z(passes[0], -0.25)
    assert len(rough_segments) > 20
    assert not _has_adjacent_reverse_segments(rough_segments)
    PREVIEWS.append(("test_adaptive_pocket_generates_ordered_closed_offset_loops", source_path, passes))


def test_pocket_ramp_entry_changes_xy_and_z():
    source_path = rectangle_source_path("p3", width=3, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = make_pocket_operation(strategy="raster", lead_in={"type": "ramp", "length": 0.5})

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    line_moves = [move for move in passes[0].moves if move.type == "line"]
    ramp_move = next(move for move in line_moves if move.x is not None and move.y is not None and move.z is not None)
    assert ramp_move.z < 0
    assert ramp_move.x is not None
    assert ramp_move.y is not None
    PREVIEWS.append(("test_pocket_ramp_entry_changes_xy_and_z", source_path, passes))


def test_pocket_without_ramp_uses_vertical_stepdowns():
    source_path = rectangle_source_path("p4", width=3, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = make_pocket_operation(strategy="raster", lead_in={"type": "line", "length": 0.5})

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    vertical_moves = [move for move in passes[0].moves if move.type == "line" and move.x is None and move.y is None]
    assert any(move.z == pytest.approx(-0.125) for move in vertical_moves)
    assert any(move.z == pytest.approx(-0.25) for move in vertical_moves)
    PREVIEWS.append(("test_pocket_without_ramp_uses_vertical_stepdowns", source_path, passes))


def test_l_shaped_raster_pocket_handles_disconnected_fill_segments():
    source_path = l_shape_source_path("p5")
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="raster", stepover_percent=40)

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    rapid_starts_by_y: dict[float, int] = {}
    for move in passes[0].moves:
        if move.type == "rapid" and move.x is not None and move.y is not None:
            rapid_starts_by_y[round(move.y, 3)] = rapid_starts_by_y.get(round(move.y, 3), 0) + 1
    assert any(count > 1 for count in rapid_starts_by_y.values())
    assert not passes[0].warnings
    PREVIEWS.append(("test_l_shaped_raster_pocket_handles_disconnected_fill_segments", source_path, passes))


def test_l_shaped_offset_pocket_handles_disconnected_fill_segments():
    source_path = l_shape_source_path("p5-offset")
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="offset", stepover_percent=40)

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert passes[0].kind == "pocket_clear"
    assert not passes[0].warnings
    assert len(_xy_line_moves(passes[0])) > 20
    PREVIEWS.append(("test_l_shaped_offset_pocket_handles_disconnected_fill_segments", source_path, passes))


def test_offset_triangle_pocket_does_not_backtrack_outer_loop_at_same_depth():
    source_path = triangle_source_path("p-triangle")
    tool = make_test_tool(diameter=0.1875, depth_per_pass=0.094)
    operation = make_pocket_operation(
        strategy="offset",
        stepover_percent=40,
        roughing={"depth_per_pass": 0.094, "side_allowance": 0.004, "bottom_allowance": 0.004},
        finishing={"enabled": True, "side": True, "bottom": True, "passes": 1, "milling_direction": "climb"},
    )

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    rough_segments = _xy_segments_at_z(passes[0], -0.094)
    assert len(rough_segments) > 6
    assert not _has_adjacent_reverse_segments(rough_segments)
    PREVIEWS.append(("test_offset_triangle_pocket_does_not_backtrack_outer_loop_at_same_depth", source_path, passes))


def test_pocket_warns_when_tool_is_too_large():
    source_path = rectangle_source_path("p6", width=1.0, height=0.39)
    tool = make_test_tool(diameter=0.4, depth_per_pass=0.1)
    operation = make_pocket_operation(strategy="raster")

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert all(toolpath_pass.warnings for toolpath_pass in passes)
    assert all(toolpath_pass.moves == [] for toolpath_pass in passes)
    PREVIEWS.append(("test_pocket_warns_when_tool_is_too_large", source_path, passes))


def test_pocket_tool_that_barely_fits_still_generates_small_path():
    source_path = rectangle_source_path("p7", width=1.0, height=0.41)
    tool = make_test_tool(diameter=0.4, depth_per_pass=0.1)
    operation = make_pocket_operation(strategy="raster", stepover_percent=50)

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert not passes[0].warnings
    xs = [move.x for move in passes[0].moves if move.type in {"rapid", "line"} and move.x is not None]
    ys = [move.y for move in passes[0].moves if move.type in {"rapid", "line"} and move.y is not None]
    assert max(xs) - min(xs) == pytest.approx(0.6)
    assert max(ys) - min(ys) <= 0.02
    bottom_return_moves = [
        move
        for move in passes[0].moves
        if move.type == "line" and move.x == pytest.approx(0.2) and move.z == pytest.approx(-0.1)
    ]
    assert bottom_return_moves
    wall_finish = next(toolpath_pass for toolpath_pass in passes if toolpath_pass.kind == "pocket_wall_finish")
    assert wall_finish.moves
    assert not wall_finish.warnings
    PREVIEWS.append(("test_pocket_tool_that_barely_fits_still_generates_small_path", source_path, passes))


def test_pocket_wall_finish_uses_inner_square_contour_for_square_pocket():
    source_path = rectangle_source_path("p8", width=3, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(
        strategy="raster",
        finishing={"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
    )

    passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    wall_finish = next(toolpath_pass for toolpath_pass in passes if toolpath_pass.kind == "pocket_wall_finish")
    assert any(move.type == "line" and move.x is not None and move.y is not None for move in wall_finish.moves)
    assert not any(move.type == "arc" for move in wall_finish.moves)
    PREVIEWS.append(("test_pocket_wall_finish_uses_inner_square_contour_for_square_pocket", source_path, passes))


def _xy_line_moves(toolpath_pass):
    return [move for move in toolpath_pass.moves if move.type == "line" and move.x is not None and move.y is not None]


def make_pocket_operation(**overrides) -> PocketOperation:
    data = {
        "id": "op-pocket",
        "type": "pocket",
        "entity": "pocket-entity",
        "tool": "t5",
        "depth": 0.25,
        "strategy": "raster",
        "stepover_percent": 50,
        "roughing": {
            "enabled": True,
            "depth_per_pass": 0.125,
            "side_allowance": 0.0,
            "bottom_allowance": 0.0,
            "milling_direction": "climb",
        },
        "finishing": {
            "enabled": True,
            "side": True,
            "bottom": True,
            "passes": 1,
            "milling_direction": "climb",
        },
        "lead_in": {"type": "ramp", "length": 0.5},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return PocketOperation.model_validate(data)


def rectangle_source_path(entity: str, width: float, height: float) -> SourcePath:
    points = [
        Point2D(x=0, y=0),
        Point2D(x=width, y=0),
        Point2D(x=width, y=height),
        Point2D(x=0, y=height),
    ]
    return path_from_points(entity, points)


def l_shape_source_path(entity: str) -> SourcePath:
    points = [
        Point2D(x=0, y=0),
        Point2D(x=3, y=0),
        Point2D(x=3, y=1),
        Point2D(x=1, y=1),
        Point2D(x=1, y=3),
        Point2D(x=0, y=3),
    ]
    return path_from_points(entity, points)


def triangle_source_path(entity: str) -> SourcePath:
    points = [
        Point2D(x=0, y=0),
        Point2D(x=1.0, y=0),
        Point2D(x=0.5, y=0.866),
    ]
    return path_from_points(entity, points)


def path_from_points(entity: str, points: list[Point2D]) -> SourcePath:
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceLineSegment(type="line", start=start, end=end)
            for start, end in zip(points, [*points[1:], points[0]], strict=True)
        ],
    )


def _xy_segments_at_z(toolpath_pass, z_value: float):
    segments = []
    current_x = None
    current_y = None
    current_z = None
    for move in toolpath_pass.moves:
        if move.type == "rapid":
            current_x = move.x if move.x is not None else current_x
            current_y = move.y if move.y is not None else current_y
            current_z = move.z if move.z is not None else current_z
            continue
        if move.type not in {"line", "arc"}:
            continue
        start = (current_x, current_y, current_z)
        current_x = move.x if move.x is not None else current_x
        current_y = move.y if move.y is not None else current_y
        current_z = move.z if move.z is not None else current_z
        end = (current_x, current_y, current_z)
        if (
            None not in start
            and None not in end
            and start[2] == pytest.approx(z_value)
            and end[2] == pytest.approx(z_value)
            and (start[0], start[1]) != pytest.approx((end[0], end[1]))
        ):
            segments.append((_round_xy(start), _round_xy(end)))
    return segments


def _has_adjacent_reverse_segments(segments) -> bool:
    return any(first == (second[1], second[0]) for first, second in zip(segments, segments[1:], strict=False))


def _round_xy(point):
    return (round(float(point[0]), 4), round(float(point[1]), 4))


def make_test_tool(diameter: float, depth_per_pass: float) -> Tool:
    return Tool.model_validate(
        {
            "id": "t5",
            "description": "test tool",
            "end_type": "flat",
            "flute_spiral": "upcut",
            "diameter": diameter,
            "flutes": 2,
            "speed": 18000,
            "feed_rate": 60,
            "plunge_rate": 20,
            "depth_per_pass": depth_per_pass,
        }
    )
