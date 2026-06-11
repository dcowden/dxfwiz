from pathlib import Path
import re

import pytest

from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import ContourOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.operations import (
    assert_mostly_offset,
    contour_operation_to_toolpaths,
    render_toolpath_preview_sheet_svg,
    source_path_points,
)
from dxfwiz.toolpaths.model import SourceArcSegment, SourceLineSegment, SourcePath


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
PREVIEWS = []


def teardown_module():
    if PREVIEWS:
        render_toolpath_preview_sheet_svg(PREVIEWS, OUTPUT_DIR / "test_contour_operations.svg")


def test_contour_operation_generates_rough_depth_passes_and_finish_pass():
    source_path = rectangle_source_path("e1", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op1",
            "type": "contour",
            "entity": "e1",
            "tool": "t5",
            "depth": 0.25,
            "extra_depth": 0.01,
            "offset": "outside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.08,
                "side_allowance": 0.01,
                "bottom_allowance": 0.0,
                "milling_direction": "conventional",
            },
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    pass_points = [
        (move.x, move.y)
        for move in passes[0].moves
        if getattr(move, "x", None) is not None and getattr(move, "y", None) is not None
    ]
    assert pass_points[0] == pytest.approx(pass_points[-1])
    assert [toolpath_pass.kind for toolpath_pass in passes] == [
        "rough_contour",
        "finish_contour",
    ]
    assert [toolpath_pass.z_bottom for toolpath_pass in passes] == pytest.approx([-0.26, -0.26])
    assert [toolpath_pass.offset_distance for toolpath_pass in passes] == pytest.approx([0.135, 0.125])
    rough_z_values = _line_move_z_values(passes[0])
    rough_xy_values = _line_move_xy_values(passes[0])
    assert [z for z, xy in zip(rough_z_values, rough_xy_values, strict=True) if xy == (None, None)] == pytest.approx(
        [-0.08, -0.16, -0.26]
    )
    assert passes[0].milling_direction == "conventional"
    assert passes[-1].milling_direction == "climb"
    PREVIEWS.append(("test_contour_operation_generates_rough_depth_passes_and_finish_pass", source_path, passes))


def test_contour_without_ramping_uses_vertical_stepdowns():
    source_path = rectangle_source_path("e1", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op-vertical",
            "type": "contour",
            "entity": "e1",
            "tool": "t5",
            "depth": 0.25,
            "offset": "outside",
            "ramping": False,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert len(passes) == 1
    z_values = _line_move_z_values(passes[0])
    xy_values = _line_move_xy_values(passes[0])
    vertical_stepdowns = [
        z
        for z, xy in zip(z_values, xy_values, strict=True)
        if xy == (None, None)
    ]
    assert vertical_stepdowns == pytest.approx([-0.125, -0.25])
    PREVIEWS.append(("test_contour_without_ramping_uses_vertical_stepdowns", source_path, passes))
    svg = render_toolpath_preview_sheet_svg(
        [("test_contour_without_ramping_uses_vertical_stepdowns", source_path, passes)],
        OUTPUT_DIR / "test_contour_without_ramping_uses_vertical_stepdowns.svg",
    )
    assert "op-vertical-rough-iso" in svg
    match = re.search(r'data-pass="op-vertical-rough-iso"[^>]+points="([^"]+)"', svg)
    assert match is not None
    iso_points = [
        tuple(float(value) for value in point.split(","))
        for point in match.group(1).split()
    ]
    assert iso_points[0][0] == pytest.approx(iso_points[1][0])
    assert iso_points[0][1] != pytest.approx(iso_points[1][1])


def test_contour_with_ramping_generates_connected_spiral_descent():
    source_path = rectangle_source_path("e3", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op-spiral",
            "type": "contour",
            "entity": "e3",
            "tool": "t5",
            "depth": 0.25,
            "extra_depth": 0.01,
            "offset": "outside",
            "ramping": True,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.08,
                "side_allowance": 0.01,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.id for toolpath_pass in passes] == ["op-spiral-rough-spiral"]
    assert passes[0].z_bottom == pytest.approx(-0.26)
    z_values = _line_move_z_values(passes[0])
    xy_values = _line_move_xy_values(passes[0])
    assert z_values[0] == pytest.approx(0.0)
    assert z_values[-1] == pytest.approx(-0.26)
    assert all(next_z <= z + 1e-9 for z, next_z in zip(z_values, z_values[1:], strict=False))
    assert all(x is not None and y is not None for x, y in xy_values[1:])
    assert len(xy_values) > 17
    assert sum(1 for z in z_values if z == pytest.approx(-0.26)) > 1
    PREVIEWS.append(("test_contour_with_ramping_generates_connected_spiral_descent", source_path, passes))


def test_contour_with_ramping_and_finish_does_not_duplicate_bottom_cleanup():
    source_path = rectangle_source_path("e4", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op-spiral-finish",
            "type": "contour",
            "entity": "e4",
            "tool": "t5",
            "depth": 0.25,
            "offset": "outside",
            "ramping": True,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.01,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["rough_contour", "finish_contour"]
    assert len(_line_move_xy_values(passes[0])) > 9
    assert sum(1 for z in _line_move_z_values(passes[0]) if z == pytest.approx(-0.25)) == 1
    PREVIEWS.append(("test_contour_with_ramping_and_finish_does_not_duplicate_bottom_cleanup", source_path, passes))


def test_contour_tabs_are_hopped_only_on_depths_below_tab_top_without_ramping():
    source_path = rectangle_source_path("tabs-e1", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.1)
    operation = ContourOperation.model_validate(
        {
            "id": "op-tabs",
            "type": "contour",
            "entity": "tabs-e1",
            "tool": "t5",
            "depth": 0.25,
            "offset": "on",
            "ramping": False,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.1,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
            "tabs": tabs_on_bottom_edge(height=0.1),
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    z_values = _line_move_z_values(passes[0])
    assert not any(z == pytest.approx(-0.15) for z in z_values[:6])
    assert any(z == pytest.approx(-0.15) for z in z_values)
    deep_segments = _xy_segments_at_z(passes[0], -0.25)
    assert not any(_segment_crosses_tab_span(segment, 1.38, 2.62) for segment in deep_segments)
    tab_top_segments = _xy_segments_at_z(passes[0], -0.15)
    assert any(_segment_crosses_tab_span(segment, 1.38, 2.62) for segment in tab_top_segments)
    PREVIEWS.append(("test_contour_tabs_are_hopped_only_on_depths_below_tab_top_without_ramping", source_path, passes))


def test_contour_tabs_apply_to_finish_passes():
    source_path = rectangle_source_path("tabs-finish", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = ContourOperation.model_validate(
        {
            "id": "op-tabs-finish",
            "type": "contour",
            "entity": "tabs-finish",
            "tool": "t5",
            "depth": 0.25,
            "offset": "on",
            "roughing": {
                "enabled": False,
                "depth_per_pass": 0.25,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
            "tabs": tabs_on_bottom_edge(height=0.1),
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["finish_contour"]
    assert any(move.z == pytest.approx(-0.15) for move in passes[0].moves if move.type == "line")
    assert not any(_segment_crosses_tab_span(segment, 1.38, 2.62) for segment in _xy_segments_at_z(passes[0], -0.25))
    PREVIEWS.append(("test_contour_tabs_apply_to_finish_passes", source_path, passes))


def test_contour_tabs_with_ramping_reenter_by_backing_up_to_far_side_of_tab():
    source_path = rectangle_source_path("tabs-ramp", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = ContourOperation.model_validate(
        {
            "id": "op-tabs-ramp",
            "type": "contour",
            "entity": "tabs-ramp",
            "tool": "t5",
            "depth": 0.25,
            "offset": "on",
            "ramping": True,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.25,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
            "tabs": tabs_on_bottom_edge(height=0.1),
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    line_moves = [move for move in passes[0].moves if move.type == "line"]
    bottom_moves = [(move.x, move.y, move.z) for move in line_moves if move.x is not None and move.y is not None]
    assert any(move.z == pytest.approx(-0.15) for move in line_moves)
    assert any(
        first[0] is not None
        and second[0] is not None
        and first[1] == pytest.approx(0.0)
        and second[1] == pytest.approx(0.0)
        and second[0] < first[0]
        and second[2] < first[2]
        for first, second in zip(bottom_moves, bottom_moves[1:], strict=False)
    )
    PREVIEWS.append(("test_contour_tabs_with_ramping_reenter_by_backing_up_to_far_side_of_tab", source_path, passes))


def test_contour_offset_validation_uses_realized_toolpath_moves():
    source_path = rectangle_source_path("e1", width=4, height=2)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = ContourOperation.model_validate(
        {
            "id": "op1",
            "type": "contour",
            "entity": "e1",
            "tool": "t5",
            "depth": 0.25,
            "offset": "outside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.25,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert_mostly_offset(source_path, passes[0], expected_distance=0.125, tolerance=1e-3)
    xy_moves = [
        (move.x, move.y)
        for move in passes[0].moves
        if move.type == "line" and move.x is not None and move.y is not None
    ]
    arc_moves = [move for move in passes[0].moves if move.type == "arc"]
    assert len(arc_moves) == 4
    assert len(xy_moves) <= len(source_path.segments)
    PREVIEWS.append(("test_contour_offset_validation_uses_realized_toolpath_moves", source_path, passes))


def test_contour_operation_uses_tool_feed_rate_by_default():
    source_path = rectangle_source_path("feed-default", width=2, height=1)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op-feed-default",
            "type": "contour",
            "entity": "feed-default",
            "tool": "t5",
            "depth": 0.125,
            "offset": "outside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert passes[0].feed_rate == pytest.approx(tool.feed_rate)


def test_contour_operation_honors_feed_rate_override():
    source_path = rectangle_source_path("feed-override", width=2, height=1)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op-feed-override",
            "type": "contour",
            "entity": "feed-override",
            "tool": "t5",
            "depth": 0.125,
            "feed_rate": 42.0,
            "offset": "outside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert passes[0].feed_rate == pytest.approx(42.0)
    assert all(move.feed == pytest.approx(42.0) for move in passes[0].moves if move.type in {"line", "arc"})


def test_contour_operation_handles_arc_and_line_profile():
    source_path = arc_and_line_source_path("e2")
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = ContourOperation.model_validate(
        {
            "id": "op2",
            "type": "contour",
            "entity": "e2",
            "tool": "t5",
            "depth": 0.125,
            "offset": "outside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert len(source_path_points(source_path)) > len(source_path.segments)
    assert [toolpath_pass.kind for toolpath_pass in passes] == ["rough_contour"]
    assert_mostly_offset(source_path, passes[0], expected_distance=0.125, tolerance=0.015)
    PREVIEWS.append(("test_contour_operation_handles_arc_and_line_profile", source_path, passes))


def test_inside_contour_generates_tiny_path_when_tool_barely_fits_and_warns_when_too_big():
    tool = make_test_tool(diameter=0.4, depth_per_pass=0.1)
    slot_that_fits = rectangle_source_path("e5", width=1.0, height=0.41)
    fits_operation = ContourOperation.model_validate(
        {
            "id": "op-inside-fits",
            "type": "contour",
            "entity": "e5",
            "tool": "t5",
            "depth": 0.1,
            "offset": "inside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.1,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": False},
        }
    )

    passes = contour_operation_to_toolpaths(fits_operation, slot_that_fits, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["rough_contour"]
    assert passes[0].warnings == []
    fit_xy_moves = [
        (move.x, move.y)
        for move in passes[0].moves
        if move.type == "line" and move.x is not None and move.y is not None
    ]
    xs = [point[0] for point in fit_xy_moves]
    ys = [point[1] for point in fit_xy_moves]
    assert max(xs) - min(xs) == pytest.approx(0.6)
    assert max(ys) - min(ys) == pytest.approx(0.01)
    PREVIEWS.append(("test_inside_contour_generates_tiny_path_when_tool_barely_fits", slot_that_fits, passes))

    slot_too_small = rectangle_source_path("e6", width=1.0, height=0.39)
    too_big_operation = ContourOperation.model_validate(
        {
            "id": "op-inside-too-big",
            "type": "contour",
            "entity": "e6",
            "tool": "t5",
            "depth": 0.1,
            "offset": "inside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.1,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )

    too_big_passes = contour_operation_to_toolpaths(too_big_operation, slot_too_small, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in too_big_passes] == ["rough_contour", "finish_contour"]
    assert all(toolpath_pass.warnings for toolpath_pass in too_big_passes)
    assert all(toolpath_pass.moves == [] for toolpath_pass in too_big_passes)
    PREVIEWS.append(("test_inside_contour_warns_when_tool_does_not_fit", slot_too_small, too_big_passes))


def _line_move_z_values(toolpath_pass):
    return [move.z for move in toolpath_pass.moves if move.type == "line"]


def _line_move_xy_values(toolpath_pass):
    return [(move.x, move.y) for move in toolpath_pass.moves if move.type == "line"]


def _xy_segments_at_z(toolpath_pass, z_value: float):
    segments = []
    current_x = None
    current_y = None
    current_z = None
    for move in toolpath_pass.moves:
        if move.type == "rapid":
            if move.x is not None:
                current_x = move.x
            if move.y is not None:
                current_y = move.y
            if move.z is not None:
                current_z = move.z
            continue
        if move.type != "line":
            continue
        start = (current_x, current_y, current_z)
        if move.x is not None:
            current_x = move.x
        if move.y is not None:
            current_y = move.y
        if move.z is not None:
            current_z = move.z
        end = (current_x, current_y, current_z)
        if None not in start and None not in end and start[2] == pytest.approx(z_value) and end[2] == pytest.approx(z_value):
            segments.append((start, end))
    return segments


def _segment_crosses_tab_span(segment, start_x: float, end_x: float) -> bool:
    first, second = segment
    if first[1] != pytest.approx(0.0) or second[1] != pytest.approx(0.0):
        return False
    low = min(first[0], second[0])
    high = max(first[0], second[0])
    return low < end_x and high > start_x


def rectangle_source_path(entity: str, width: float, height: float) -> SourcePath:
    points = [
        Point2D(x=0, y=0),
        Point2D(x=width, y=0),
        Point2D(x=width, y=height),
        Point2D(x=0, y=height),
    ]
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceLineSegment(type="line", start=points[0], end=points[1]),
            SourceLineSegment(type="line", start=points[1], end=points[2]),
            SourceLineSegment(type="line", start=points[2], end=points[3]),
            SourceLineSegment(type="line", start=points[3], end=points[0]),
        ],
    )


def arc_and_line_source_path(entity: str) -> SourcePath:
    points = [
        Point2D(x=0, y=0),
        Point2D(x=4, y=0),
        Point2D(x=4, y=1),
        Point2D(x=0, y=1),
    ]
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceLineSegment(type="line", start=points[0], end=points[1]),
            SourceLineSegment(type="line", start=points[1], end=points[2]),
            SourceArcSegment(
                type="arc",
                start=points[2],
                end=points[3],
                center=Point2D(x=2, y=1),
                radius=2,
                direction="ccw",
            ),
            SourceLineSegment(type="line", start=points[3], end=points[0]),
        ],
    )


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


def tabs_on_bottom_edge(height: float = 0.1):
    return {
        "enabled": True,
        "width": 1.0,
        "height": height,
        "count": 1,
        "locations": [
            {
                "center": {"x": 2.0, "y": 0.0},
                "lower_left": {"x": 1.5, "y": -0.1},
                "upper_right": {"x": 2.5, "y": 0.1},
                "width": 1.0,
                "height": height,
                "angle_deg": 0.0,
            }
        ],
    }
