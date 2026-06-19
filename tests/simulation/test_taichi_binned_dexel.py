from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
from html import escape
import json
import math
from pathlib import Path
import re

import numpy as np
import pytest

from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.schemas.job import JobFile
from dxfwiz.simulation import build_expected_removals, simulate_toolpath
from dxfwiz.simulation.engine import _arc_points
from dxfwiz.simulation.model import DexelSimulationRequest, SimulationBounds, SimulationSettings, SimulationStock
from dxfwiz.simulation.taichi_binned import (
    BinnedDexelSettings,
    _display_classes,
    binned_dexel_class_image,
    render_binned_dexel_map_png,
    taichi_available,
    validate_binned_dexels,
)
from tests.camotics_validation import CAMOTICS_RESOLUTION_MM
from dxfwiz.svg import render_geometry_svg
from dxfwiz.toolpaths import ToolpathRequest, generate_toolpaths
from dxfwiz.toolpaths.model import ArcMove, DwellMove, LineMove, RapidMove, ToolpathPass, ToolpathPlan
from dxfwiz.yaml_io import load_yaml_file


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"
SIMULATION_OUTPUT_DIR = OUTPUT_DIR / "simulation"
DEXEL_RESOLUTION_IN = 0.002
CAMOTICS_RESOLUTION_IN = CAMOTICS_RESOLUTION_MM / 25.4


pytestmark = pytest.mark.skipif(not taichi_available(), reason="Taichi is not installed")


def test_taichi_binned_dexel_matches_cpu_for_swept_line():
    request = _small_request(
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
        expected=[
            {
                "type": "swept_line",
                "start_x": 0.25,
                "start_y": 0.5,
                "end_x": 1.75,
                "end_y": 0.5,
                "start_depth": 0.1,
                "end_depth": 0.1,
                "radius": 0.125,
            }
        ],
    )

    cpu_run = simulate_toolpath(request)
    taichi_run = validate_binned_dexels(
        request,
        BinnedDexelSettings(xy_spacing=0.05, tile_size=8, backend="auto"),
    )

    assert taichi_run.metrics.simulation.removed_cells == cpu_run.response.metrics.removed_cells
    assert taichi_run.metrics.simulation.expected_removed_cells == cpu_run.response.metrics.expected_removed_cells
    assert taichi_run.metrics.simulation.overcut_cells == 0
    assert taichi_run.metrics.simulation.undercut_cells == 0
    assert taichi_run.metrics.simulation.max_actual_depth == pytest.approx(0.1)


def test_taichi_binned_screw_clearance_map_marks_drilled_center_and_undercut_ring():
    request = _screw_clearance_request()

    run = validate_binned_dexels(
        request,
        BinnedDexelSettings(xy_spacing=0.002, tile_size=32, backend="auto", capture_map=True),
    )
    assert run.validation_map is not None
    class_codes = run.validation_map.class_codes

    assert np.count_nonzero(class_codes == 2) > 0
    assert np.count_nonzero(class_codes == 5) > 0
    assert np.count_nonzero(class_codes == 6) == 0

    png_path = SIMULATION_OUTPUT_DIR / "screw_clearance_undercut_map.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(
        render_binned_dexel_map_png(run, max_dimension=None, include_legend=True, mode="correctness")
    )
    assert png_path.stat().st_size > 1000


def test_taichi_binned_flags_rapid_collision_cells():
    request = _small_request(
        [
            _pass(
                "op-rapid-collision",
                [
                    {"type": "rapid", "x": 0.25, "y": 0.25, "z": 0.25},
                    {"type": "line", "z": -0.1, "feed": 10},
                    {"type": "line", "x": 0.75, "y": 0.25, "z": -0.1, "feed": 40},
                    {"type": "rapid", "x": 1.5, "y": 1.5, "z": -0.1},
                ],
            )
        ],
    )

    run = validate_binned_dexels(
        request,
        BinnedDexelSettings(xy_spacing=0.05, tile_size=8, backend="auto", capture_map=True),
    )

    assert run.validation_map is not None
    assert run.metrics.simulation.rapid_collision_count > 0
    assert np.count_nonzero(run.validation_map.class_codes == 7) > 0


def test_taichi_binned_diagnostic_palette_collapses_recuts_to_green():
    display, palette = _display_classes(np.array([[2, 3, 4, 5, 6, 7]], dtype=np.uint8), "diagnostic")

    assert tuple(palette[display[0, 2]]) == (245, 158, 11)
    assert tuple(palette[display[0, 3]]) == (249, 115, 22)
    assert tuple(palette[display[0, 4]]) == (220, 38, 38)
    assert tuple(palette[display[0, 5]]) == (127, 29, 29)
    assert tuple(palette[display[0, 0]]) == (22, 163, 74)
    assert tuple(palette[display[0, 1]]) == (22, 163, 74)


def _real_dxf_cases():
    from tests.test_dxf_cleaner_integration import real_dxf_cases

    return real_dxf_cases()


@pytest.mark.integration
@pytest.mark.parametrize("case", _real_dxf_cases(), ids=lambda case: case.name)
def test_all_real_dxf_cases_taichi_binned_dexel_validation_writes_timings(case):
    request = _simulation_request_for_case(case)

    run = validate_binned_dexels(
        request,
        BinnedDexelSettings(xy_spacing=DEXEL_RESOLUTION_IN, tile_size=64, backend="auto", capture_map=True),
    )
    area_counts = _validation_area_counts(run)
    csv_path = case.output_dir / "dexel_timings.csv"
    png_path = case.output_dir / "dexel_map.png"
    diagnostic_png_path = case.output_dir / "diagnostic_map.png"
    _write_timing_csv(csv_path, run, area_counts=area_counts)
    png_path.write_bytes(
        render_binned_dexel_map_png(run, max_dimension=4096, include_legend=True, mode="correctness")
    )
    diagnostic_png_path.write_bytes(
        render_binned_dexel_map_png(run, max_dimension=4096, include_legend=True, mode="diagnostic")
    )

    assert run.metrics.active_tiles > 0
    assert run.metrics.valid_active_cells > 0
    assert run.metrics.actual_primitives > 0
    assert run.metrics.expected_primitives > 0
    assert run.validation_map is not None
    assert run.metrics.simulation.rapid_collision_count == 0
    assert csv_path.exists()
    assert png_path.stat().st_size > 1000
    assert diagnostic_png_path.stat().st_size > 1000
    _write_dashboard(_real_dxf_cases())


def _simulation_request_for_case(case) -> DexelSimulationRequest:
    from tests.test_dxf_cleaner_integration import (
        FakePlannerClient,
        _planning_request,
        ensure_case_outputs,
    )

    ensure_case_outputs(case)
    machine = MachineFile.model_validate(load_yaml_file(case.machine_path))
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    geometry = GeometryFile.model_validate(load_yaml_file(case.geom_path))
    planning_request = _planning_request(case, geometry, machine, planner)
    plan_response = generate_operation_plan_for_test(planning_request, FakePlannerClient())
    case.op_path.write_text(plan_response.op_yaml, encoding="utf-8")
    job = JobFile.model_validate(plan_response.plan)
    simulated_geometry = GeometryFile.model_validate(plan_response.geometry)
    render_geometry_svg(case.geom_path, case.fixed_path, case.svg_path)
    toolpath_response = generate_toolpaths(
        ToolpathRequest(
            job=job,
            geometry=simulated_geometry,
            machine=machine,
            fixed_dxf=case.fixed_path.read_text(encoding="utf-8", errors="ignore"),
        )
    )
    assert toolpath_response.plan is not None
    case.gcode_path.write_text(toolpath_response.gcode, encoding="utf-8")

    expected = build_expected_removals(
        job,
        simulated_geometry,
        toolpath_response.plan,
        xy_spacing=DEXEL_RESOLUTION_IN,
        arc_chord_fraction=1.0,
    )
    _write_build_summary(
        case,
        job,
        machine,
        toolpath_response.plan,
        planning_errors=plan_response.errors,
        planning_warnings=plan_response.warnings,
        toolpath_errors=toolpath_response.errors,
        toolpath_warnings=toolpath_response.warnings,
        expected_warnings=expected.warnings,
    )
    return DexelSimulationRequest(
        job=job,
        machine=machine,
        toolpath_plan=toolpath_response.plan,
        stock=_stock_from_geometry(simulated_geometry, planner),
        settings=SimulationSettings(xy_spacing=DEXEL_RESOLUTION_IN, preview=False, arc_chord_fraction=1.0),
        expected_removals=expected.removals,
    )

def generate_operation_plan_for_test(planning_request, client):
    from dxfwiz.planning import generate_operation_plan

    response = generate_operation_plan(planning_request, client=client)
    assert response.errors == []
    assert response.plan is not None
    assert response.geometry is not None
    return response


def _write_build_summary(
    case,
    job: JobFile,
    machine: MachineFile,
    plan: ToolpathPlan,
    *,
    planning_errors,
    planning_warnings,
    toolpath_errors,
    toolpath_warnings,
    expected_warnings: list[str],
) -> None:
    summary = {
        "operation_counts": dict(sorted(Counter(operation.type for operation in job.operations).items())),
        "estimated_machining_seconds": _estimate_machining_seconds(plan, machine),
        "kinematics": _kinematics_summary(machine),
        "issues": _grouped_issue_summary(
            planning_errors=planning_errors,
            planning_warnings=planning_warnings,
            toolpath_errors=toolpath_errors,
            toolpath_warnings=toolpath_warnings,
            expected_warnings=expected_warnings,
        ),
    }
    (case.output_dir / "build_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )


def _estimate_machining_seconds(plan: ToolpathPlan, machine: MachineFile) -> float:
    position = (0.0, 0.0, float(machine.machine.clear_z))
    total = 0.0
    for move in plan.commands:
        position, seconds = _estimate_move_time(position, move, machine, feed_rate=None)
        total += seconds
    for toolpath_pass in plan.passes:
        feed_rate = toolpath_pass.feed_rate
        for move in toolpath_pass.moves:
            if isinstance(move, LineMove) and move.feed is not None:
                feed_rate = move.feed
            elif isinstance(move, ArcMove) and move.feed is not None:
                feed_rate = move.feed
            position, seconds = _estimate_move_time(position, move, machine, feed_rate=feed_rate)
            total += seconds
    return total


def _estimate_move_time(
    position: tuple[float, float, float],
    move,
    machine: MachineFile,
    *,
    feed_rate: float | None,
) -> tuple[tuple[float, float, float], float]:
    if isinstance(move, RapidMove):
        next_position = _move_next_position(position, move)
        return next_position, _segment_time(position, next_position, machine, feed_rate=None)
    if isinstance(move, LineMove):
        next_position = _move_next_position(position, move)
        return next_position, _segment_time(position, next_position, machine, feed_rate=feed_rate)
    if isinstance(move, ArcMove):
        points = _arc_points(position, move, 0.5, DEXEL_RESOLUTION_IN)
        seconds = 0.0
        start = position
        for end in points:
            seconds += _segment_time(start, end, machine, feed_rate=feed_rate)
            start = end
        return (points[-1] if points else _move_next_position(position, move)), seconds
    if isinstance(move, DwellMove):
        return position, float(move.seconds)
    return position, 0.0


def _segment_time(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    machine: MachineFile,
    *,
    feed_rate: float | None,
) -> float:
    delta = (end[0] - start[0], end[1] - start[1], end[2] - start[2])
    distance = math.sqrt(sum(value * value for value in delta))
    if distance <= 1e-12:
        return 0.0
    max_velocity = _path_limit(machine.machine.kinematics.max_velocity, delta, distance)
    acceleration = _path_limit(machine.machine.kinematics.acceleration, delta, distance)
    if feed_rate is not None:
        max_velocity = min(max_velocity, feed_rate / 60.0)
    return _trapezoid_time(distance, max_velocity, acceleration)


def _path_limit(limits, delta: tuple[float, float, float], distance: float) -> float:
    values = (limits.x, limits.y, limits.z)
    candidates = [
        value / component
        for value, axis_delta in zip(values, delta, strict=True)
        if (component := abs(axis_delta) / distance) > 1e-12
    ]
    return min(candidates) if candidates else min(values)


def _trapezoid_time(distance: float, max_velocity: float, acceleration: float) -> float:
    max_velocity = max(max_velocity, 1e-9)
    acceleration = max(acceleration, 1e-9)
    accel_time = max_velocity / acceleration
    accel_distance = 0.5 * acceleration * accel_time * accel_time
    if distance >= 2 * accel_distance:
        return 2 * accel_time + (distance - 2 * accel_distance) / max_velocity
    return 2 * math.sqrt(distance / acceleration)


def _move_next_position(position: tuple[float, float, float], move) -> tuple[float, float, float]:
    return (
        float(move.x) if getattr(move, "x", None) is not None else position[0],
        float(move.y) if getattr(move, "y", None) is not None else position[1],
        float(move.z) if getattr(move, "z", None) is not None else position[2],
    )


def _kinematics_summary(machine: MachineFile) -> dict:
    kinematics = machine.machine.kinematics
    return {
        "max_velocity_in_per_sec": kinematics.max_velocity.model_dump(mode="json"),
        "acceleration_in_per_sec2": kinematics.acceleration.model_dump(mode="json"),
    }


def _grouped_issue_summary(
    *,
    planning_errors,
    planning_warnings,
    toolpath_errors,
    toolpath_warnings,
    expected_warnings: list[str],
) -> dict:
    errors = [*_issues(planning_errors, "planning"), *_issues(toolpath_errors, "toolpath")]
    warnings = [
        *_issues(planning_warnings, "planning"),
        *_issues(toolpath_warnings, "toolpath"),
        *[{"code": "W_EXPECTED", "message": message, "source": "simulation"} for message in expected_warnings],
    ]
    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": _group_issues(errors),
        "warnings": _group_issues(warnings),
    }


def _issues(items, source: str) -> list[dict[str, str]]:
    return [
        {
            "code": getattr(item, "code", "UNKNOWN"),
            "message": getattr(item, "message", str(item)),
            "source": source,
        }
        for item in items
    ]


def _group_issues(items: list[dict[str, str]]) -> list[dict[str, str | int]]:
    grouped: dict[tuple[str, str], dict[str, str | int]] = {}
    for item in items:
        code = item["code"]
        message = _normalize_issue_message(item["message"])
        key = (code, message)
        if key not in grouped:
            grouped[key] = {"count": 0, "code": code, "message": message}
        grouped[key]["count"] = int(grouped[key]["count"]) + 1
    return sorted(grouped.values(), key=lambda row: (-int(row["count"]), str(row["code"])))


def _normalize_issue_message(message: str) -> str:
    message = " ".join(str(message).split())
    message = re.sub(r"\bline \d+:", "line <n>:", message)
    message = re.sub(r"^op[\w-]+:\s*", "", message)
    return message


def _write_timing_csv(path: Path, run, *, area_counts: dict[str, int] | None = None) -> None:
    area_counts = area_counts or {"overunder_area_count": 0, "collision_area_count": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "backend",
                "xy_spacing",
                "full_grid_width",
                "full_grid_height",
                "active_tiles",
                "active_cells",
                "valid_active_cells",
                "actual_primitives",
                "expected_primitives",
                "primitive_tile_refs",
                "preprocess_seconds",
                "binning_seconds",
                "kernel_seconds",
                "total_seconds",
                "removed_cells",
                "expected_removed_cells",
                "overcut_cells",
                "undercut_cells",
                "overunder_area_count",
                "collision_area_count",
                "recut_cells",
                "excessive_recut_cells",
                "rapid_collision_count",
                "unsafe_rapid_count",
                "max_cut_count",
                "max_actual_depth",
                "max_expected_depth",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "backend": run.metrics.backend,
                "xy_spacing": run.metrics.simulation.xy_spacing,
                "full_grid_width": run.metrics.full_grid_width,
                "full_grid_height": run.metrics.full_grid_height,
                "active_tiles": run.metrics.active_tiles,
                "active_cells": run.metrics.active_cells,
                "valid_active_cells": run.metrics.valid_active_cells,
                "actual_primitives": run.metrics.actual_primitives,
                "expected_primitives": run.metrics.expected_primitives,
                "primitive_tile_refs": run.metrics.primitive_tile_refs,
                "preprocess_seconds": run.timings.preprocess_seconds,
                "binning_seconds": run.timings.binning_seconds,
                "kernel_seconds": run.timings.kernel_seconds,
                "total_seconds": run.timings.total_seconds,
                "removed_cells": run.metrics.simulation.removed_cells,
                "expected_removed_cells": run.metrics.simulation.expected_removed_cells,
                "overcut_cells": run.metrics.simulation.overcut_cells,
                "undercut_cells": run.metrics.simulation.undercut_cells,
                "overunder_area_count": area_counts["overunder_area_count"],
                "collision_area_count": area_counts["collision_area_count"],
                "recut_cells": run.metrics.simulation.recut_cells,
                "excessive_recut_cells": run.metrics.simulation.excessive_recut_cells,
                "rapid_collision_count": run.metrics.simulation.rapid_collision_count,
                "unsafe_rapid_count": run.metrics.simulation.unsafe_rapid_count,
                "max_cut_count": run.metrics.simulation.max_cut_count,
                "max_actual_depth": run.metrics.simulation.max_actual_depth,
                "max_expected_depth": run.metrics.simulation.max_expected_depth,
            }
        )


def _validation_area_counts(run) -> dict[str, int]:
    class_image = binned_dexel_class_image(run, max_dimension=4096)
    return {
        "overunder_area_count": _connected_area_count((class_image == 5) | (class_image == 6)),
        "collision_area_count": _connected_area_count(class_image == 7),
    }


def _connected_area_count(mask: np.ndarray) -> int:
    remaining = np.asarray(mask, dtype=bool).copy()
    areas = 0
    height, width = remaining.shape
    while True:
        points = np.argwhere(remaining)
        if points.size == 0:
            return areas
        areas += 1
        stack = [tuple(int(value) for value in points[0])]
        remaining[stack[0]] = False
        while stack:
            y, x = stack.pop()
            if y > 0 and remaining[y - 1, x]:
                remaining[y - 1, x] = False
                stack.append((y - 1, x))
            if y + 1 < height and remaining[y + 1, x]:
                remaining[y + 1, x] = False
                stack.append((y + 1, x))
            if x > 0 and remaining[y, x - 1]:
                remaining[y, x - 1] = False
                stack.append((y, x - 1))
            if x + 1 < width and remaining[y, x + 1]:
                remaining[y, x + 1] = False
                stack.append((y, x + 1))


def _write_dashboard(cases) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_path = OUTPUT_DIR / "index.html"
    case_cards = "\n".join(_dashboard_case(case) for case in cases)
    nav_links = "\n".join(_dashboard_nav_link(case) for case in cases)
    dashboard_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DXF Wizard Test Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172033;
      --muted: #667085;
      --line: #d9e0e4;
      --panel: #f7f9f9;
      --ok: #139447;
      --warn: #b45309;
      --bad: #b42318;
      --link: #145db3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: #fff;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 4;
      display: grid;
      gap: 8px;
      padding: 14px 18px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(10px);
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .suite-title {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 10px;
    }}
    .meta {{ color: var(--muted); font-size: 12px; }}
    nav {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding-bottom: 2px;
    }}
    nav a, .links a {{
      color: var(--link);
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }}
    nav a {{
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
    }}
    nav a.status-ok {{ border-color: #bbf7d0; background: #f0fdf4; color: #166534; }}
    nav a.status-warn {{ border-color: #fde68a; background: #fffbeb; color: #92400e; }}
    nav a.status-fail {{ border-color: #fecaca; background: #fef2f2; color: #991b1b; }}
    a:hover {{ border-bottom-color: currentColor; }}
    main {{
      display: grid;
      gap: 18px;
      padding: 18px;
    }}
    section.case {{
      min-height: min(920px, calc(100vh - 84px));
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: clip;
      background: #fff;
    }}
    .case-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .case-head h2 {{
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      justify-content: flex-end;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      min-height: 24px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .pill strong {{ color: var(--ink); font-weight: 650; }}
    .pill.ok strong {{ color: var(--ok); }}
    .pill.warn strong {{ color: var(--warn); }}
    .pill.bad strong {{ color: var(--bad); }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .status-ok {{ color: #166534; background: #dcfce7; border: 1px solid #bbf7d0; }}
    .status-warn {{ color: #92400e; background: #fef3c7; border: 1px solid #fde68a; }}
    .status-fail {{ color: #991b1b; background: #fee2e2; border: 1px solid #fecaca; }}
    .case-grid {{
      display: grid;
      grid-template-columns: minmax(220px, 0.9fr) minmax(360px, 1.25fr) minmax(260px, 0.85fr);
      gap: 12px;
      padding: 12px;
    }}
    .panel {{
      min-width: 0;
      display: grid;
      gap: 8px;
      align-content: start;
    }}
    .panel h3 {{
      margin: 0;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}
    .preview {{
      width: 100%;
      height: 260px;
      object-fit: contain;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .maps {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    figure {{
      margin: 0;
      min-width: 0;
    }}
    figcaption {{
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    .map-img {{
      width: 100%;
      height: 300px;
      object-fit: contain;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .links {{
      display: grid;
      gap: 8px;
    }}
    .build-cards {{
      grid-column: 1 / span 2;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .build-card {{
      display: grid;
      gap: 10px;
      align-content: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .build-card h3 {{
      margin: 0;
      font-size: 14px;
      color: var(--ink);
      text-transform: none;
      letter-spacing: 0;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 6px;
    }}
    .stat {{
      padding: 7px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    .stat span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .stat strong {{
      display: block;
      margin-top: 2px;
      font-size: 16px;
    }}
    .issue-list {{
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .issue-list li {{
      padding: 6px 7px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    .issue-list code {{
      font-weight: 700;
    }}
    .link-group {{
      display: grid;
      gap: 4px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
    }}
    .link-group strong {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .link-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px 10px;
    }}
    details {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    summary {{
      cursor: pointer;
      padding: 7px 8px;
      color: var(--link);
    }}
    pre {{
      max-height: 210px;
      overflow: auto;
      margin: 0;
      padding: 8px;
      border-top: 1px solid var(--line);
      background: #fbfcfc;
      font-size: 12px;
      white-space: pre-wrap;
    }}
    .missing {{ color: var(--muted); font-style: italic; }}
    @media (max-width: 1180px) {{
      .case-grid {{ grid-template-columns: 1fr; }}
      .maps {{ grid-template-columns: 1fr; }}
      .build-cards {{ grid-column: auto; grid-template-columns: 1fr; }}
      .preview, .map-img {{ height: 240px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <div class="suite-title">
        <h1>DXF Wizard Test Dashboard</h1>
        <span class="pill">dexel_resolution <strong>{DEXEL_RESOLUTION_IN:.4f} in</strong></span>
        <span class="pill">camotics_resolution <strong>{CAMOTICS_RESOLUTION_IN:.4f} in</strong></span>
      </div>
      <div class="meta">Generated {escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}. Excessive recut threshold is 4 or more planned cuts.</div>
    </div>
    <nav aria-label="Case links">
      {nav_links}
    </nav>
  </header>
  <main>
    {case_cards}
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def _dashboard_case(case) -> str:
    timing = _read_timing(case.output_dir / "dexel_timings.csv")
    build_summary = _read_build_summary(case.output_dir / "build_summary.json")
    status = _ci_status(timing)
    geometry_svg = _relative_dashboard_link(case.svg_path)
    dexel_map = _relative_dashboard_link(case.output_dir / "dexel_map.png")
    diagnostic_map = _relative_dashboard_link(case.output_dir / "diagnostic_map.png")
    geometry_image = _image_or_missing(case.svg_path, geometry_svg, "Geometry preview", "preview")
    map_image = _image_or_missing(case.output_dir / "dexel_map.png", dexel_map, "Dexel map", "map-img")
    diagnostic_image = _image_or_missing(
        case.output_dir / "diagnostic_map.png",
        diagnostic_map,
        "Diagnostic map",
        "map-img",
    )
    pills = _metric_pills(timing)
    yaml_panel = _yaml_details(case)
    artifact_links = _artifact_links(case)
    camotics_links = _camotics_links(case)
    operation_card = _operation_stats_card(build_summary)
    issue_card = _issue_summary_card(build_summary)
    return f"""<section class="case" id="{escape(case.name)}">
  <div class="case-head">
    <h2>{_status_pill(status)} {escape(case.name)}</h2>
    <div class="summary">{pills}</div>
  </div>
  <div class="case-grid">
    <div class="panel">
      <h3>Geometry</h3>
      {geometry_image}
    </div>
    <div class="panel">
      <h3>Simulation Maps</h3>
      <div class="maps">
        <figure>
          <figcaption>Correctness</figcaption>
          {map_image}
        </figure>
        <figure>
          <figcaption>Diagnostic</figcaption>
          {diagnostic_image}
        </figure>
      </div>
    </div>
    <div class="panel">
      <h3>Files</h3>
      <div class="links">
        {yaml_panel}
        {camotics_links}
        {artifact_links}
      </div>
    </div>
    <div class="build-cards">
      {operation_card}
      {issue_card}
    </div>
  </div>
</section>"""


def _dashboard_nav_link(case) -> str:
    timing = _read_timing(case.output_dir / "dexel_timings.csv")
    status = _ci_status(timing)
    return (
        f'<a class="{status["css"]}" href="#{escape(case.name)}">'
        f'<span>{escape(status["mark"])} {escape(status["name"])}</span>'
        f'<span>{escape(case.name)}</span></a>'
    )


def _metric_pills(timing: dict[str, str]) -> str:
    if not timing:
        return '<span class="pill">simulation <strong>pending</strong></span>'
    excessive = int(float(timing.get("excessive_recut_cells", "0") or 0))
    collisions = int(float(timing.get("rapid_collision_count", "0") or 0))
    collision_areas = int(float(timing.get("collision_area_count", collisions) or 0))
    overcut = int(float(timing.get("overcut_cells", "0") or 0))
    undercut = int(float(timing.get("undercut_cells", "0") or 0))
    overunder_areas = int(float(timing.get("overunder_area_count", overcut + undercut) or 0))
    recut_class = "ok" if excessive == 0 else "bad"
    collision_class = "ok" if collision_areas == 0 and collisions == 0 else "bad"
    cut_class = "ok" if overunder_areas == 0 and overcut == 0 and undercut == 0 else "warn"
    return "".join(
        [
            _pill("total", _format_seconds(timing.get("total_seconds"))),
            _pill("kernel", _format_seconds(timing.get("kernel_seconds"))),
            _pill("dexel_resolution", f'{timing.get("xy_spacing", "?")} in'),
            _pill("active", _format_int(timing.get("active_cells"))),
            _pill("over/under areas", _format_int(overunder_areas), cut_class),
            _pill("collision areas", _format_int(collision_areas), collision_class),
            _pill("excessive", _format_int(excessive), recut_class),
            _pill("max cuts", _format_int(timing.get("max_cut_count"))),
        ]
    )


def _ci_status(timing: dict[str, str]) -> dict[str, str]:
    if not timing:
        return {"name": "PENDING", "mark": "...", "css": "status-warn"}
    excessive = int(float(timing.get("excessive_recut_cells", "0") or 0))
    collisions = int(float(timing.get("rapid_collision_count", "0") or 0))
    collision_areas = int(float(timing.get("collision_area_count", collisions) or 0))
    overcut = int(float(timing.get("overcut_cells", "0") or 0))
    undercut = int(float(timing.get("undercut_cells", "0") or 0))
    overunder_areas = int(float(timing.get("overunder_area_count", overcut + undercut) or 0))
    if collision_areas > 0 or collisions > 0 or excessive > 0:
        return {"name": "FAIL", "mark": "X", "css": "status-fail"}
    if overunder_areas > 0 or overcut > 0 or undercut > 0:
        return {"name": "WARN", "mark": "!", "css": "status-warn"}
    return {"name": "PASS", "mark": "✓", "css": "status-ok"}


def _status_pill(status: dict[str, str]) -> str:
    return (
        f'<span class="status-pill {status["css"]}">'
        f'{escape(status["mark"])} {escape(status["name"])}</span>'
    )


def _pill(label: str, value: str, css_class: str = "") -> str:
    class_attr = f"pill {css_class}".strip()
    return f'<span class="{class_attr}">{escape(label)} <strong>{escape(value)}</strong></span>'


def _artifact_links(case) -> str:
    links = [
        ("geometry svg", case.svg_path),
        ("fixed dxf", case.fixed_path),
        ("nc", case.gcode_path),
        ("timings csv", case.output_dir / "dexel_timings.csv"),
    ]
    return _link_group("Primary Artifacts", links)


def _operation_stats_card(summary: dict) -> str:
    if not summary:
        return '<div class="build-card"><h3>Operation Statistics</h3><span class="missing">Pending</span></div>'
    operation_counts = summary.get("operation_counts", {})
    operation_stats = "".join(
        f'<div class="stat"><span>{escape(str(name))}</span><strong>{_format_int(count)}</strong></div>'
        for name, count in operation_counts.items()
    )
    if not operation_stats:
        operation_stats = '<span class="missing">No operations</span>'
    kinematics = summary.get("kinematics", {})
    velocity = kinematics.get("max_velocity_in_per_sec", {})
    acceleration = kinematics.get("acceleration_in_per_sec2", {})
    return f"""<div class="build-card">
  <h3>Operation Statistics</h3>
  <div class="stat-grid">
    <div class="stat"><span>estimated machining time</span><strong>{_format_duration(summary.get("estimated_machining_seconds"))}</strong></div>
    <div class="stat"><span>max velocity</span><strong>{_format_axis_values(velocity)} in/s</strong></div>
    <div class="stat"><span>acceleration</span><strong>{_format_axis_values(acceleration)} in/s²</strong></div>
  </div>
  <div class="stat-grid">{operation_stats}</div>
</div>"""


def _issue_summary_card(summary: dict) -> str:
    if not summary:
        return '<div class="build-card"><h3>Errors and Warnings</h3><span class="missing">Pending</span></div>'
    issues = summary.get("issues", {})
    error_count = int(issues.get("error_count", 0))
    warning_count = int(issues.get("warning_count", 0))
    errors = _issue_rows(issues.get("errors", []), "error")
    warnings = _issue_rows(issues.get("warnings", []), "warning")
    if not errors:
        errors = '<li class="missing">No errors</li>'
    if not warnings:
        warnings = '<li class="missing">No warnings</li>'
    return f"""<div class="build-card">
  <h3>Errors and Warnings</h3>
  <div class="summary">
    {_pill("errors", _format_int(error_count), "bad" if error_count else "ok")}
    {_pill("warnings", _format_int(warning_count), "warn" if warning_count else "ok")}
  </div>
  <ul class="issue-list">{errors}</ul>
  <ul class="issue-list">{warnings}</ul>
</div>"""


def _issue_rows(items: list[dict], severity: str) -> str:
    css_class = "bad" if severity == "error" else "warn"
    return "".join(
        f'<li><span class="pill {css_class}"><strong>{_format_int(item.get("count", 0))}x</strong></span> '
        f'<code>{escape(str(item.get("code", "UNKNOWN")))}</code>: '
        f'{escape(str(item.get("message", "")))}</li>'
        for item in items[:6]
    )


def _format_axis_values(values: dict) -> str:
    if not values:
        return "?"
    return "/".join(f"{float(values.get(axis, 0.0)):g}" for axis in ("x", "y", "z"))


def _format_duration(value) -> str:
    if value in (None, ""):
        return "?"
    seconds = float(value)
    minutes = int(seconds // 60)
    remaining = seconds - minutes * 60
    if minutes:
        return f"{minutes}m {remaining:04.1f}s"
    return f"{remaining:.1f}s"


def _yaml_details(case) -> str:
    paths = [
        ("operation yaml", case.op_path),
        ("geometry yaml", case.geom_path),
        ("camotics summary", case.camotics_summary_path),
        ("machine yaml", case.machine_path),
        ("planner yaml", case.planner_path),
        ("operation assertions", case.operation_assertions_path),
        ("gcode assertions", case.gcode_assertions_path),
        ("simulation assertions", case.simulation_assertions_path),
    ]
    details = []
    for label, path in paths:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            details.append(
                f"""<details>
  <summary>{escape(label)} - <a href="{_relative_dashboard_link(path)}">open</a></summary>
  <pre>{escape(text[:12000])}</pre>
</details>"""
            )
    if not details:
        return '<div class="link-group"><strong>YAML</strong><span class="missing">No YAML artifacts yet</span></div>'
    return '<div class="link-group"><strong>YAML</strong>' + "\n".join(details) + "</div>"


def _camotics_links(case) -> str:
    if not case.camotics_output_dir.exists():
        return '<div class="link-group"><strong>CAMotics</strong><span class="missing">No CAMotics artifacts yet</span></div>'
    rows = []
    for operation_dir in sorted(path for path in case.camotics_output_dir.iterdir() if path.is_dir()):
        operation_links = []
        for name in ["report.html", "simulated.png", "simulated.stl", "generated.nc", "project.camotics"]:
            path = operation_dir / name
            if path.exists():
                operation_links.append((name, path))
        if operation_links:
            rows.append(
                f'<div><strong>{escape(operation_dir.name)}</strong> '
                f'<span class="link-list">{_links(operation_links)}</span></div>'
            )
    if not rows:
        return '<div class="link-group"><strong>CAMotics</strong><span class="missing">No CAMotics artifacts yet</span></div>'
    return '<div class="link-group"><strong>CAMotics</strong>' + "\n".join(rows) + "</div>"


def _link_group(title: str, links: list[tuple[str, Path]]) -> str:
    existing = [(label, path) for label, path in links if path.exists()]
    if not existing:
        return f'<div class="link-group"><strong>{escape(title)}</strong><span class="missing">Pending</span></div>'
    return (
        f'<div class="link-group"><strong>{escape(title)}</strong>'
        f'<span class="link-list">{_links(existing)}</span></div>'
    )


def _links(links: list[tuple[str, Path]]) -> str:
    return " ".join(
        f'<a href="{_relative_dashboard_link(path)}">{escape(label)}</a>' for label, path in links
    )


def _image_or_missing(path: Path, href: str, alt: str, css_class: str) -> str:
    if not path.exists():
        return f'<div class="{css_class} missing">Pending</div>'
    return f'<a href="{href}"><img class="{css_class}" src="{href}" alt="{escape(alt)}"></a>'


def _read_timing(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        return next(csv.DictReader(handle), {})


def _read_build_summary(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _relative_dashboard_link(path: Path) -> str:
    return escape(Path(_relative_path(OUTPUT_DIR, path)).as_posix())


def _relative_path(start: Path, target: Path) -> str:
    start_parts = start.resolve().parts
    target_parts = target.resolve().parts
    common = 0
    for left, right in zip(start_parts, target_parts, strict=False):
        if left.lower() != right.lower():
            break
        common += 1
    up = [".."] * (len(start_parts) - common)
    down = list(target_parts[common:])
    return str(Path(*up, *down)) if up or down else "."


def _format_seconds(value: str | None) -> str:
    if value in (None, ""):
        return "?"
    return f"{float(value):.2f}s"


def _format_int(value) -> str:
    if value in (None, ""):
        return "?"
    return f"{int(float(value)):,}"


def _stock_from_geometry(geometry: GeometryFile, planner: PlannerFile) -> SimulationStock:
    stock = planner.defaults.stock
    assert stock is not None
    box = geometry.summary.bounding_box
    return SimulationStock(
        bounds=SimulationBounds(
            min_x=box.min.x,
            min_y=box.min.y,
            max_x=box.max.x,
            max_y=box.max.y,
        ),
        thickness=stock.thickness,
    )


def _small_request(passes: list[ToolpathPass], expected: list[dict] | None = None) -> DexelSimulationRequest:
    return DexelSimulationRequest.model_validate(
        {
            "machine": _machine().model_dump(mode="json"),
            "toolpath_plan": ToolpathPlan(units="in", coordinate_system="G55", passes=passes).model_dump(mode="json"),
            "stock": _small_stock().model_dump(mode="json"),
            "settings": {"xy_spacing": 0.05, "z_spacing": 0.02, "preview": False},
            "expected_removals": expected or [],
        }
    )


def _screw_clearance_request() -> DexelSimulationRequest:
    return DexelSimulationRequest.model_validate(
        {
            "machine": _machine(diameter=0.1875).model_dump(mode="json"),
            "toolpath_plan": ToolpathPlan(
                units="in",
                coordinate_system="G55",
                passes=[
                    _pass(
                        "op-screw",
                        [
                            {"type": "rapid", "x": 0.3, "y": 0.3, "z": 0.25},
                            {"type": "line", "z": -0.25, "feed": 10},
                        ],
                        tool_diameter=0.1875,
                    )
                ],
            ).model_dump(mode="json"),
            "stock": {
                "bounds": {"min_x": 0.0, "min_y": 0.0, "max_x": 0.6, "max_y": 0.6},
                "thickness": 0.25,
            },
            "settings": {"xy_spacing": 0.002, "z_spacing": 0.02, "preview": False},
            "expected_removals": [
                {
                    "type": "circle",
                    "operation_id": "op-screw",
                    "entity": "wh1",
                    "center_x": 0.3,
                    "center_y": 0.3,
                    "radius": 0.125,
                    "depth": 0.25,
                }
            ],
        }
    )


def _small_stock() -> SimulationStock:
    return SimulationStock(
        bounds=SimulationBounds(min_x=0, min_y=0, max_x=2, max_y=2),
        thickness=0.25,
    )


def _pass(operation_id: str, moves: list[dict], tool_diameter: float = 0.25) -> ToolpathPass:
    return ToolpathPass.model_validate(
        {
            "id": f"{operation_id}-pass",
            "operation_id": operation_id,
            "entity": "e1",
            "kind": "pocket_clear",
            "tool": "t1",
            "tool_diameter": tool_diameter,
            "feed_rate": 40,
            "z_bottom": -0.1,
            "moves": moves,
        }
    )


def _machine(diameter: float = 0.25) -> MachineFile:
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
                    "description": "flat endmill",
                    "end_type": "flat",
                    "flute_spiral": "upcut",
                    "diameter": diameter,
                    "flutes": 2,
                    "speed": 18000,
                    "feed_rate": 40,
                    "plunge_rate": 10,
                }
            ],
        }
    )
