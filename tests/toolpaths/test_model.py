import pytest
from pydantic import TypeAdapter, ValidationError

from dxfwiz.schemas.common import Point2D
from dxfwiz.toolpaths.model import ArcMove, SourceArcSegment, SourceLineSegment, SourcePath, ToolpathMove, ToolpathPass, ToolpathPlan


def test_source_path_preserves_lines_and_arcs():
    path = SourcePath(
        id="path-e1",
        entity="e1",
        closed=True,
        segments=[
            SourceLineSegment(
                type="line",
                start=Point2D(x=0, y=0),
                end=Point2D(x=1, y=0),
            ),
            SourceArcSegment(
                type="arc",
                start=Point2D(x=1, y=0),
                end=Point2D(x=0, y=1),
                center=Point2D(x=0, y=0),
                radius=1,
                direction="ccw",
            ),
        ],
    )

    assert path.segments[0].type == "line"
    assert path.segments[1].type == "arc"
    assert path.segments[1].direction == "ccw"


def test_toolpath_pass_carries_assertion_metadata_and_postable_moves():
    toolpath_pass = ToolpathPass(
        id="tp1",
        operation_id="op1",
        entity="e1",
        kind="rough_contour",
        tool="t5",
        tool_diameter=0.25,
        z_top=0,
        z_bottom=-0.08,
        source_path="path-e1",
        offset_side="outside",
        offset_distance=0.135,
        milling_direction="conventional",
        moves=[
            {"type": "rapid", "x": 0, "y": 0, "z": 0.5},
            {"type": "line", "z": -0.08, "feed": 20},
            {"type": "arc", "direction": "ccw", "x": 1, "y": 0, "z": -0.08, "i": 0.5, "j": 0, "feed": 60},
        ],
    )

    assert toolpath_pass.offset_distance == pytest.approx(0.135)
    assert isinstance(toolpath_pass.moves[2], ArcMove)
    assert toolpath_pass.moves[2].z == pytest.approx(-0.08)


def test_move_union_rejects_spindle_on_without_rpm():
    adapter = TypeAdapter(ToolpathMove)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "spindle", "state": "on"})


def test_toolpath_plan_groups_source_paths_and_passes():
    plan = ToolpathPlan(
        units="in",
        coordinate_system="G55",
        source_paths=[
            {
                "id": "path-e1",
                "entity": "e1",
                "closed": True,
                "segments": [
                    {
                        "type": "line",
                        "start": {"x": 0, "y": 0},
                        "end": {"x": 1, "y": 0},
                    }
                ],
            }
        ],
        passes=[
            {
                "id": "tp1",
                "operation_id": "op1",
                "entity": "e1",
                "kind": "finish_contour",
                "tool": "t5",
                "tool_diameter": 0.25,
                "z_bottom": -0.26,
                "offset_side": "outside",
                "offset_distance": 0.125,
            }
        ],
    )

    assert plan.schema_version == "1.0"
    assert plan.source_paths[0].segments[0].type == "line"
    assert plan.passes[0].kind == "finish_contour"
