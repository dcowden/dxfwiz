from __future__ import annotations

from dxfwiz.gcode import lift_gcode, validate_gcode_against_plan
from dxfwiz.toolpaths.model import ToolpathPass, ToolpathPlan
from dxfwiz.toolpaths.posts.uccnc import UccncPost


def test_lifter_expands_modal_motion_and_arcs():
    gcode = "\n".join(
        [
            "G20",
            "G90",
            "G17",
            "G94",
            "G55",
            "G0 X0 Y0 Z0.25",
            "G1 Z-0.1 F20",
            "X1 F60",
            "G3 X0 Y0 I-0.5 J0",
            "M30",
        ]
    )

    lifted = lift_gcode(gcode)

    assert lifted.units == "in"
    assert lifted.coordinate_system == "G55"
    assert [move.kind for move in lifted.moves if move.kind in {"rapid", "line", "arc", "program_end"}] == [
        "rapid",
        "line",
        "line",
        "arc",
        "program_end",
    ]
    arc = next(move for move in lifted.moves if move.kind == "arc")
    assert arc.direction == "ccw"
    assert arc.center == (0.5, -0.0)
    assert arc.radius == 0.5
    assert not lifted.warnings


def test_validation_round_trips_posted_plan_to_canonical_moves():
    plan = _simple_plan(program_end=True)
    gcode = UccncPost(precision=4).render(plan)

    issues = validate_gcode_against_plan(gcode, plan, safe_z=0.25)

    assert issues == []


def test_validation_flags_rapid_xy_below_safe_z():
    plan = _simple_plan(program_end=True)
    gcode = "\n".join(
        [
            "G20",
            "G90",
            "G17",
            "G94",
            "G55",
            "G0 Z0.25",
            "G0 X0 Y0",
            "G1 Z-0.1 F20",
            "G0 X1 Y1",
            "M30",
        ]
    )

    issues = validate_gcode_against_plan(gcode, plan, safe_z=0.25)

    assert any(issue.code == "rapid_xy_below_safe_z" for issue in issues)


def test_validation_flags_arc_radius_mismatch():
    gcode = "\n".join(
        [
            "G20",
            "G90",
            "G17",
            "G94",
            "G55",
            "G0 X0 Y0 Z0.25",
            "G1 Z-0.1 F20",
            "G3 X1 Y0 I0 J0.5",
            "M30",
        ]
    )

    lifted = lift_gcode(gcode, tolerance=1e-6)

    assert any("arc radius mismatch" in warning for warning in lifted.warnings)


def _simple_plan(*, program_end: bool) -> ToolpathPlan:
    moves = [
        {"type": "rapid", "x": 0.0, "y": 0.0, "z": 0.25},
        {"type": "line", "z": -0.1, "feed": 20.0},
        {"type": "line", "x": 1.0, "y": 0.0, "feed": 60.0},
        {"type": "arc", "direction": "ccw", "x": 0.0, "y": 0.0, "i": -0.5, "j": 0.0, "feed": 60.0},
    ]
    if program_end:
        moves.append({"type": "program_end"})
    return ToolpathPlan.model_validate(
        {
            "units": "in",
            "coordinate_system": "G55",
            "commands": [
                {"type": "units", "length": "in"},
                {"type": "distance_mode", "mode": "absolute"},
                {"type": "plane", "plane": "xy"},
                {"type": "feed_mode", "mode": "units_per_min"},
                {"type": "coordinate_system", "code": "G55"},
            ],
            "passes": [
                {
                    "id": "p1",
                    "operation_id": "op1",
                    "kind": "trace",
                    "tool": "t5",
                    "tool_diameter": 0.25,
                    "feed_rate": 60.0,
                    "z_top": 0.0,
                    "z_bottom": -0.1,
                    "moves": moves,
                }
            ],
        }
    )
