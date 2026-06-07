from pathlib import Path

import pytest

from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import DrillOperation, HelicalDrillOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.drilling import drill_operation_to_toolpaths, helical_drill_operation_to_toolpaths
from dxfwiz.toolpaths.operations import render_toolpath_preview_sheet_svg
from dxfwiz.toolpaths.model import SourceArcSegment, SourcePath


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
PREVIEWS = []


def teardown_module():
    if PREVIEWS:
        render_toolpath_preview_sheet_svg(PREVIEWS, OUTPUT_DIR / "test_drilling_operations.svg")


def test_drill_operation_generates_vertical_peck_moves_only():
    operation = DrillOperation.model_validate(
        {
            "id": "op-drill",
            "type": "drill",
            "entity": "e-hole",
            "tool": "t5",
            "depth": 0.3,
            "peck_depth": 0.125,
            "retract_amount": 0.04,
            "dwell_time": 0.1,
        }
    )
    tool = make_test_tool(diameter=0.125, depth_per_pass=0.125)

    passes = drill_operation_to_toolpaths(operation, center=(1.0, 1.0), tool=tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["peck_drill"]
    line_moves = [move for move in passes[0].moves if move.type == "line"]
    assert [move.z for move in line_moves] == pytest.approx([-0.125, -0.25, -0.3])
    assert all(move.x is None and move.y is None for move in line_moves)
    assert sum(1 for move in passes[0].moves if move.type == "dwell") == 3
    PREVIEWS.append(("test_drill_operation_generates_vertical_peck_moves_only", circle_source_path("e-hole", (1.0, 1.0), 0.125), passes))


def test_helical_drill_uses_arc_moves_for_rough_and_finish():
    operation = HelicalDrillOperation.model_validate(
        {
            "id": "op-helix",
            "type": "helical_drill",
            "entity": "e-bore",
            "tool": "t5",
            "depth": 0.26,
            "pitch": 0.08,
            "milling_direction": "climb",
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.08)

    passes = helical_drill_operation_to_toolpaths(
        operation,
        center=(1.0, 1.0),
        hole_diameter=0.75,
        tool=tool,
        safe_z=0.5,
        finishing_allowance=0.01,
    )

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["helical_drill", "finish_contour"]
    rough_arcs = [move for move in passes[0].moves if move.type == "arc"]
    finish_arcs = [move for move in passes[1].moves if move.type == "arc"]
    assert rough_arcs
    assert finish_arcs
    assert all(move.direction == "ccw" for move in rough_arcs + finish_arcs)
    assert rough_arcs[-1].z == pytest.approx(-0.26)
    assert finish_arcs[0].z == pytest.approx(-0.26)
    assert passes[0].warnings == []
    PREVIEWS.append(("test_helical_drill_uses_arc_moves_for_rough_and_finish", circle_source_path("e-bore", (1.0, 1.0), 0.75), passes))


def test_helical_drill_warns_when_interior_slug_is_large():
    operation = HelicalDrillOperation.model_validate(
        {
            "id": "op-large-bore",
            "type": "helical_drill",
            "entity": "e-large-bore",
            "tool": "t5",
            "depth": 0.25,
            "pitch": 0.08,
            "milling_direction": "climb",
            "finishing": {"enabled": False},
        }
    )
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.08)

    passes = helical_drill_operation_to_toolpaths(
        operation,
        center=(1.0, 1.0),
        hole_diameter=2.0,
        tool=tool,
        safe_z=0.5,
    )

    assert passes[0].kind == "helical_drill"
    assert passes[0].warnings
    assert "slug" in passes[0].warnings[0]
    assert any(move.type == "arc" for move in passes[0].moves)
    PREVIEWS.append(("test_helical_drill_warns_when_interior_slug_is_large", circle_source_path("e-large-bore", (1.0, 1.0), 2.0), passes))


def test_helical_drill_warns_when_tool_is_too_large():
    operation = HelicalDrillOperation.model_validate(
        {
            "id": "op-too-small",
            "type": "helical_drill",
            "entity": "e-too-small",
            "tool": "t5",
            "depth": 0.25,
            "pitch": 0.08,
            "milling_direction": "climb",
            "finishing": {"enabled": False},
        }
    )
    tool = make_test_tool(diameter=0.5, depth_per_pass=0.08)

    passes = helical_drill_operation_to_toolpaths(
        operation,
        center=(1.0, 1.0),
        hole_diameter=0.4,
        tool=tool,
        safe_z=0.5,
    )

    assert passes[0].warnings
    assert passes[0].moves == []
    PREVIEWS.append(("test_helical_drill_warns_when_tool_is_too_large", circle_source_path("e-too-small", (1.0, 1.0), 0.4), passes))


def circle_source_path(entity: str, center: tuple[float, float], diameter: float) -> SourcePath:
    radius = diameter / 2
    cx, cy = center
    points = [
        Point2D(x=cx + radius, y=cy),
        Point2D(x=cx, y=cy + radius),
        Point2D(x=cx - radius, y=cy),
        Point2D(x=cx, y=cy - radius),
    ]
    center_point = Point2D(x=cx, y=cy)
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceArcSegment(type="arc", start=points[0], end=points[1], center=center_point, radius=radius, direction="ccw"),
            SourceArcSegment(type="arc", start=points[1], end=points[2], center=center_point, radius=radius, direction="ccw"),
            SourceArcSegment(type="arc", start=points[2], end=points[3], center=center_point, radius=radius, direction="ccw"),
            SourceArcSegment(type="arc", start=points[3], end=points[0], center=center_point, radius=radius, direction="ccw"),
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
