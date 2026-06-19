from pathlib import Path

import numpy as np
import pytest

from dxfwiz.schemas import GeometryFile, MachineFile
from dxfwiz.schemas.job import JobFile
from dxfwiz.simulation import (
    DexelGrid,
    DexelSimulationRequest,
    SimulationBounds,
    SimulationSettings,
    SimulationStock,
    build_expected_removals,
    render_dexel_preview_png,
    simulate_toolpath,
)
from dxfwiz.toolpaths.model import ToolpathPass, ToolpathPlan


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "simulation"


def test_dexel_grid_initializes_expected_and_actual_depth_arrays():
    grid = DexelGrid.create(
        _stock(),
        SimulationSettings(xy_spacing=0.1),
        minimum_tool_diameter=0.25,
    )

    grid.expected_rectangle(0.25, 0.25, 0.75, 0.75, 0.125)
    grid.expected_circle((1.2, 1.2), 0.2, 0.2)

    assert grid.actual_depth.shape == grid.expected_depth.shape
    assert grid.actual_depth.max() == pytest.approx(0)
    assert grid.expected_depth.max() == pytest.approx(0.2)
    assert grid.operation_index("op-a") == 0
    assert grid.operation_index("op-a") == 0


def test_expected_polygon_and_swept_line_are_counted_as_expected_removal():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-contour",
                    [
                        {"type": "rapid", "x": 0.25, "y": 0.25, "z": 0.25},
                        {"type": "line", "z": -0.1, "feed": 10},
                        {"type": "line", "x": 1.75, "y": 0.25, "z": -0.1, "feed": 40},
                    ],
                )
            ],
            expected=[
                {
                    "type": "polygon",
                    "points": [{"x": 0.8, "y": 0.8}, {"x": 1.2, "y": 0.8}, {"x": 1.0, "y": 1.2}],
                    "depth": 0.05,
                },
                {
                    "type": "swept_line",
                    "start_x": 0.25,
                    "start_y": 0.25,
                    "end_x": 1.75,
                    "end_y": 0.25,
                    "start_depth": 0.1,
                    "end_depth": 0.1,
                    "radius": 0.125,
                },
            ],
        )
    )

    assert run.response.metrics.expected_removed_cells > 0
    assert run.response.metrics.max_expected_depth == pytest.approx(0.1)


def test_non_cutting_move_pass_without_tool_diameter_updates_position_without_error():
    move_pass = ToolpathPass.model_validate(
        {
            "id": "op-move-pass",
            "operation_id": "op-move",
            "kind": "move",
            "z_bottom": 0.25,
            "moves": [{"type": "rapid", "x": 1.0, "y": 1.0, "z": 0.25}],
        }
    )

    run = simulate_toolpath(_request([move_pass]))

    assert run.response.errors == []


def test_generated_screw_hole_expected_removal_uses_full_stock_depth():
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "screw sim",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "planner": "planner.yaml",
                "post": "post.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "generated_entities": [
                {
                    "id": "wh1",
                    "role": "screw_hole",
                    "shape": "circle",
                    "center": {"x": 1.0, "y": 1.0},
                    "diameter": 0.25,
                }
            ],
            "operations": [
                {
                    "id": "op-screw",
                    "type": "drill",
                    "description": "shallow screw drill",
                    "entity": "wh1",
                    "tool": "t1",
                    "depth": 0.1,
                    "peck_depth": 0.1,
                    "retract_amount": 0.25,
                }
            ],
        }
    )
    expected = build_expected_removals(job, _geometry())
    shallow_drill = _pass(
        "op-screw",
        [
            {"type": "rapid", "x": 1.0, "y": 1.0, "z": 0.25},
            {"type": "line", "z": -0.1, "feed": 10},
        ],
    )

    run = simulate_toolpath(_request([shallow_drill], expected=expected.removals))

    assert expected.removals[0]["depth"] == pytest.approx(0.25)
    assert run.response.metrics.undercut_cells > 0


def test_linear_feed_removes_swept_capsule_and_tracks_last_operation():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-line",
                    [
                        {"type": "rapid", "x": 0.25, "y": 0.5, "z": 0.25},
                        {"type": "line", "z": -0.1, "feed": 10},
                        {"type": "line", "x": 1.75, "y": 0.5, "z": -0.1, "feed": 40},
                    ],
                )
            ],
            expected=[{"type": "rectangle", "min_x": 0.25, "min_y": 0.35, "max_x": 1.75, "max_y": 0.65, "depth": 0.1}],
        )
    )

    assert run.response.errors == []
    assert run.response.metrics.removed_cells > 0
    assert run.response.metrics.max_actual_depth == pytest.approx(0.1)
    assert np.count_nonzero(run.grid.last_operation_id == 0) > 0


def test_arc_feed_removes_curved_sweep():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-arc",
                    [
                        {"type": "rapid", "x": 1.5, "y": 1.0, "z": 0.25},
                        {"type": "line", "z": -0.1, "feed": 10},
                        {"type": "arc", "direction": "ccw", "x": 0.5, "y": 1.0, "z": -0.1, "i": -0.5, "j": 0.0, "feed": 40},
                    ],
                )
            ],
            expected=[{"type": "circle", "center_x": 1.0, "center_y": 1.0, "radius": 0.65, "depth": 0.1}],
        )
    )

    assert run.response.errors == []
    assert run.response.metrics.removed_cells > 0
    assert run.response.metrics.max_actual_depth == pytest.approx(0.1)


def test_rapid_collision_is_reported_along_path():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-rapid",
                    [
                        {"type": "rapid", "x": 0.25, "y": 1.0, "z": 0.25},
                        {"type": "rapid", "x": 1.75, "y": 1.0, "z": -0.05},
                    ],
                )
            ]
        )
    )

    assert [error.code for error in run.response.errors] == ["E3002"]
    assert run.response.metrics.rapid_collision_count == 1
    assert run.response.metrics.unsafe_rapid_count == 1


def test_vertical_retract_rapid_does_not_count_as_stock_collision():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-retract",
                    [
                        {"type": "rapid", "x": 1.0, "y": 1.0, "z": 0.25},
                        {"type": "line", "z": -0.1, "feed": 10},
                        {"type": "rapid", "z": 0.25},
                    ],
                )
            ]
        )
    )

    assert run.response.errors == []
    assert run.response.metrics.rapid_collision_count == 0


def test_overlapping_pocket_style_paths_track_recut_cells_and_air_moves():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-pocket",
                    [
                        {"type": "rapid", "x": 0.3, "y": 0.4, "z": 0.25},
                        {"type": "line", "z": -0.12, "feed": 10},
                        {"type": "line", "x": 1.7, "y": 0.4, "z": -0.12, "feed": 40},
                        {"type": "line", "x": 0.3, "y": 0.55, "z": -0.12, "feed": 40},
                        {"type": "line", "x": 1.7, "y": 0.7, "z": -0.12, "feed": 40},
                        {"type": "line", "x": 0.3, "y": 0.85, "z": -0.12, "feed": 40},
                        {"type": "line", "x": 1.7, "y": 1.0, "z": -0.12, "feed": 40},
                        {"type": "line", "x": 0.3, "y": 1.15, "z": -0.12, "feed": 40},
                    ],
                )
            ],
            expected=[{"type": "rectangle", "min_x": 0.2, "min_y": 0.3, "max_x": 1.8, "max_y": 1.25, "depth": 0.12}],
        )
    )

    assert run.response.errors == []
    assert run.response.metrics.recut_cells > 0
    assert run.response.metrics.max_cut_count > 1
    assert run.response.metrics.removed_cells > 0


def test_recut_does_not_count_extra_passes_below_stock_thickness():
    moves = [
        {"type": "rapid", "x": 0.3, "y": 1.0, "z": 0.25},
        {"type": "line", "z": -0.3, "feed": 10},
        {"type": "line", "x": 1.7, "y": 1.0, "z": -0.3, "feed": 40},
        {"type": "line", "x": 0.3, "y": 1.0, "z": -0.3, "feed": 40},
    ]

    run = simulate_toolpath(_request([_pass("op-through-stock", moves)]))

    assert run.response.metrics.max_actual_depth == pytest.approx(0.25)
    assert run.response.metrics.recut_cells == 0
    assert run.response.metrics.max_cut_count == 1


def test_excessive_recut_warns_when_same_area_is_cut_four_or_more_times():
    repeated_moves = [
        {"type": "rapid", "x": 0.3, "y": 1.0, "z": 0.25},
        {"type": "line", "z": -0.1, "feed": 10},
    ]
    for _ in range(3):
        repeated_moves.extend(
            [
                {"type": "line", "x": 1.7, "y": 1.0, "z": -0.1, "feed": 40},
                {"type": "line", "x": 0.3, "y": 1.0, "z": -0.1, "feed": 40},
            ]
        )

    run = simulate_toolpath(_request([_pass("op-recut", repeated_moves)]))

    assert run.response.metrics.excessive_recut_cells > 0
    assert run.response.metrics.max_cut_count >= 4
    assert [warning.code for warning in run.response.warnings] == ["W2011"]


def test_ball_end_profile_removes_less_at_tool_edge_than_flat_profile():
    flat = DexelGrid.create(_stock(), SimulationSettings(xy_spacing=0.025), minimum_tool_diameter=0.25)
    ball = DexelGrid.create(_stock(), SimulationSettings(xy_spacing=0.025), minimum_tool_diameter=0.25)

    flat.remove_circle((1.0, 1.0), 0.125, 0.1, "flat")
    ball.remove_circle((1.0, 1.0), 0.125, 0.1, "ball", profile=_ball_profile())

    assert ball.actual_depth.max() == pytest.approx(flat.actual_depth.max(), abs=0.005)
    assert ball.actual_depth.sum() < flat.actual_depth.sum()


def test_preview_renderer_returns_png_bytes_and_tests_write_files():
    run = simulate_toolpath(
        _request(
            [
                _pass(
                    "op-preview",
                    [
                        {"type": "rapid", "x": 0.25, "y": 0.5, "z": 0.25},
                        {"type": "line", "z": -0.1, "feed": 10},
                        {"type": "line", "x": 1.75, "y": 0.5, "z": -0.1, "feed": 40},
                    ],
                )
            ],
            expected=[{"type": "rectangle", "min_x": 0.2, "min_y": 0.35, "max_x": 1.8, "max_y": 0.65, "depth": 0.1}],
        )
    )
    initial_png = render_dexel_preview_png(
        run.grid,
        "test_preview_renderer initial",
        depth=np.zeros_like(run.grid.actual_depth),
    )
    final_png = render_dexel_preview_png(run.grid, "test_preview_renderer final")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    initial_path = OUTPUT_DIR / "test_dexel_simulation_initial.png"
    final_path = OUTPUT_DIR / "test_dexel_simulation_final.png"
    initial_path.write_bytes(initial_png)
    final_path.write_bytes(final_png)

    assert initial_path.stat().st_size > 1000
    assert final_path.stat().st_size > 1000


def _stock() -> SimulationStock:
    return SimulationStock(
        bounds=SimulationBounds(min_x=0, min_y=0, max_x=2, max_y=2),
        thickness=0.25,
    )


def _geometry() -> GeometryFile:
    return GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "source": "guessed", "confidence": 1.0, "coordinate_scale": 1.0},
            "source": {"original_file": "test.dxf", "cleaned_file": "test_fixed.dxf", "format": "dxf"},
            "summary": {
                "entity_count": 0,
                "closed_count": 0,
                "open_count": 0,
                "ignored_count": 0,
                "generated_count": 1,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 2, "y": 2}},
            },
            "entity_map": [],
            "entities": [],
        }
    )


def _request(passes: list[ToolpathPass], expected: list[dict] | None = None) -> DexelSimulationRequest:
    return DexelSimulationRequest.model_validate(
        {
            "machine": _machine().model_dump(mode="json"),
            "toolpath_plan": ToolpathPlan(units="in", coordinate_system="G55", passes=passes).model_dump(mode="json"),
            "stock": _stock().model_dump(mode="json"),
            "settings": {"xy_spacing": 0.05, "z_spacing": 0.02, "preview": True},
            "expected_removals": expected or [],
        }
    )


def _pass(operation_id: str, moves: list[dict]) -> ToolpathPass:
    return ToolpathPass.model_validate(
        {
            "id": f"{operation_id}-pass",
            "operation_id": operation_id,
            "entity": "e1",
            "kind": "pocket_clear",
            "tool": "t1",
            "tool_diameter": 0.25,
            "feed_rate": 40,
            "z_bottom": -0.1,
            "moves": moves,
        }
    )


def _machine() -> MachineFile:
    return MachineFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "machine": {
                "name": "Simulation Test Router",
                "type": "router",
                "axes": 3,
                "max_tools": 1,
                "clear_z": 0.25,
                "workholding": ["tape"],
                "part_holding": [],
                "work_envelope": {
                    "x": {"min": 0, "max": 24},
                    "y": {"min": 0, "max": 24},
                    "z": {"min": -3, "max": 0},
                },
                "coordinate_system": {
                    "x_positive": "right",
                    "y_positive": "up",
                    "origin": "bottom_left",
                },
                "spindle": {"type": "er11", "max_rpm": 24000},
            },
            "tools": [
                {
                    "id": "t1",
                    "description": "1/4 flat endmill",
                    "end_type": "flat",
                    "flute_spiral": "upcut",
                    "diameter": 0.25,
                    "flutes": 2,
                    "speed": 18000,
                    "feed_rate": 40,
                    "plunge_rate": 10,
                },
                {
                    "id": "tb",
                    "description": "1/4 ball endmill",
                    "end_type": "ball",
                    "flute_spiral": "upcut",
                    "diameter": 0.25,
                    "flutes": 2,
                    "speed": 18000,
                    "feed_rate": 40,
                    "plunge_rate": 10,
                },
            ],
        }
    )


def _ball_profile():
    from dxfwiz.simulation.model import ToolProfile

    return ToolProfile(diameter=0.25, end_type="ball")
