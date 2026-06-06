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
    assert len(xy_moves) > len(source_path.segments)
    PREVIEWS.append(("test_contour_offset_validation_uses_realized_toolpath_moves", source_path, passes))


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


def test_inside_contour_offsets_round_corners_and_warn_when_tool_does_not_fit():
    source_path = rectangle_source_path("e5", width=1, height=1)
    tool_that_fits = make_test_tool(diameter=0.4, depth_per_pass=0.1)
    rough_allowance_operation = ContourOperation.model_validate(
        {
            "id": "op-inside-tight",
            "type": "contour",
            "entity": "e5",
            "tool": "t5",
            "depth": 0.1,
            "offset": "inside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.1,
                "side_allowance": 0.31,
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

    passes = contour_operation_to_toolpaths(rough_allowance_operation, source_path, tool_that_fits, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["rough_contour", "finish_contour"]
    assert passes[0].warnings
    assert passes[0].moves == []
    finish_xy_moves = [
        (move.x, move.y)
        for move in passes[1].moves
        if move.type == "line" and move.x is not None and move.y is not None
    ]
    assert len(finish_xy_moves) == len(source_path.segments)
    assert finish_xy_moves == pytest.approx([(0.2, 0.8), (0.8, 0.8), (0.8, 0.2), (0.2, 0.2)])
    assert passes[1].warnings == []
    PREVIEWS.append(("test_inside_contour_offsets_round_corners_tool_fits_finish_only", source_path, passes))

    tool_too_big = make_test_tool(diameter=1.2, depth_per_pass=0.1)
    too_big_operation = ContourOperation.model_validate(
        {
            "id": "op-inside-too-big",
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
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )

    too_big_passes = contour_operation_to_toolpaths(too_big_operation, source_path, tool_too_big, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in too_big_passes] == ["rough_contour", "finish_contour"]
    assert all(toolpath_pass.warnings for toolpath_pass in too_big_passes)
    assert all(toolpath_pass.moves == [] for toolpath_pass in too_big_passes)
    PREVIEWS.append(("test_inside_contour_offsets_round_corners_tool_too_big", source_path, too_big_passes))


def _line_move_z_values(toolpath_pass):
    return [move.z for move in toolpath_pass.moves if move.type == "line"]


def _line_move_xy_values(toolpath_pass):
    return [(move.x, move.y) for move in toolpath_pass.moves if move.type == "line"]


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
