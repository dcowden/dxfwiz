from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from shapely.geometry import Point, box

from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml
from dxfwiz.planning import PlanningRequest, generate_operation_plan, load_system_planner_advice
from dxfwiz.planning.service import PlanningResponse, _frame_or_summary_bounds, _part_clearance_polygons
from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.schemas.job import JobFile
from dxfwiz.simulation import (
    DexelSimulationRequest,
    SimulationBounds,
    SimulationSettings,
    SimulationStock,
    build_expected_removals,
    render_dexel_preview_png,
    simulate_toolpath,
)
from dxfwiz.svg import render_geometry_svg
from dxfwiz.toolpaths import ToolpathRequest, generate_toolpaths
from dxfwiz.toolpaths.model import ToolpathPlan
from dxfwiz.toolpaths.operations import contour_offset_error_samples, contour_offset_validation, source_path_points
from dxfwiz.yaml_io import dump_yaml_file, load_yaml_file


INPUT_DIR = Path(__file__).resolve().parent / "integration_tests"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


@dataclass(frozen=True)
class DxfCase:
    name: str
    input_dir: Path
    output_dir: Path
    source_path: Path
    machine_path: Path
    planner_path: Path
    operation_assertions_path: Path
    gcode_assertions_path: Path
    simulation_assertions_path: Path
    fixed_path: Path
    geom_path: Path
    svg_path: Path
    op_path: Path
    gcode_path: Path
    simulation_initial_path: Path
    simulation_final_path: Path
    simulation_summary_path: Path
    contour_error_path: Path
    recut_summary_path: Path
    recut_heatmap_path: Path


def real_dxf_cases() -> list[DxfCase]:
    if not INPUT_DIR.exists():
        return []
    cases = []
    for input_dir in sorted(path for path in INPUT_DIR.iterdir() if path.is_dir()):
        dxf_paths = sorted(
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".dxf"
        )
        assert len(dxf_paths) == 1, f"Expected one DXF in {input_dir}"
        source_path = dxf_paths[0]
        machine_path = input_dir / "machine.yaml"
        planner_path = input_dir / "planner.yaml"
        operation_assertions_path = input_dir / "operation_assertions.yaml"
        gcode_assertions_path = input_dir / "gcode_assertions.yaml"
        simulation_assertions_path = input_dir / "simulation_assertions.yaml"
        output_dir = OUTPUT_DIR / input_dir.name
        cases.append(
            DxfCase(
                name=input_dir.name,
                input_dir=input_dir,
                output_dir=output_dir,
                source_path=source_path,
                machine_path=machine_path,
                planner_path=planner_path,
                operation_assertions_path=operation_assertions_path,
                gcode_assertions_path=gcode_assertions_path,
                simulation_assertions_path=simulation_assertions_path,
                fixed_path=output_dir / f"{source_path.stem}_fixed.dxf",
                geom_path=output_dir / f"{source_path.stem}_geom.yaml",
                svg_path=output_dir / f"{source_path.stem}_geometry.svg",
                op_path=output_dir / f"{source_path.stem}_op.yaml",
                gcode_path=output_dir / f"{source_path.stem}.nc",
                simulation_initial_path=output_dir / f"{source_path.stem}_simulation_initial.png",
                simulation_final_path=output_dir / f"{source_path.stem}_simulation_final.png",
                simulation_summary_path=output_dir / f"{source_path.stem}_simulation_summary.yaml",
                contour_error_path=output_dir / f"{source_path.stem}_contour_offset_errors.png",
                recut_summary_path=output_dir / f"{source_path.stem}_recut_analysis.yaml",
                recut_heatmap_path=output_dir / f"{source_path.stem}_recut_heatmap.png",
            )
        )
    return cases


def dxf_case(name: str) -> DxfCase:
    return next(case for case in real_dxf_cases() if case.name == name)


def ensure_case_outputs(case: DxfCase) -> None:
    case.output_dir.mkdir(parents=True, exist_ok=True)
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    if not case.fixed_path.exists():
        clean_dxf(case.source_path, case.fixed_path, _clean_config(planner))
    write_geometry_yaml(
        case.fixed_path,
        case.geom_path,
        original_file=case.source_path.name,
        cleaned_file=case.fixed_path.name,
    )


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_cleaning_outputs_fixed_dxf_and_geom_yaml(case):
    case.output_dir.mkdir(parents=True, exist_ok=True)

    MachineFile.model_validate(load_yaml_file(case.machine_path))
    source_count = _entity_count(case.source_path)
    result = clean_dxf(
        case.source_path,
        case.fixed_path,
        _clean_config(PlannerFile.model_validate(load_yaml_file(case.planner_path))),
    )
    geom_data = write_geometry_yaml(
        case.fixed_path,
        case.geom_path,
        original_file=case.source_path.name,
        cleaned_file=case.fixed_path.name,
    )

    fixed_count = _entity_count(case.fixed_path)
    GeometryFile.model_validate(load_yaml_file(case.geom_path))

    assert case.fixed_path.exists()
    assert case.geom_path.exists()
    assert source_count > 0
    assert fixed_count > 0
    assert fixed_count == result.closed_loops + result.open_paths
    assert geom_data["summary"]["entity_count"] == fixed_count
    assert geom_data["summary"]["ignored_count"] >= 0
    assert geom_data["units"]["length"] == "in"


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_geometry_svg_is_written(case):
    ensure_case_outputs(case)

    svg = render_geometry_svg(case.geom_path, case.fixed_path, case.svg_path)
    geom_data = load_yaml_file(case.geom_path)

    assert case.svg_path.exists()
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert svg.count('data-entity-id="') == geom_data["summary"]["entity_count"]
    assert "shape-circle" in svg
    if _has_role(geom_data["entity_map"], "frame"):
        assert "role-frame" in svg


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_operation_plan_outputs_op_yaml(case):
    ensure_case_outputs(case)
    machine = MachineFile.model_validate(load_yaml_file(case.machine_path))
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    geometry = GeometryFile.model_validate(load_yaml_file(case.geom_path))
    assertions = load_yaml_file(case.operation_assertions_path)

    planning_request = _planning_request(case, geometry, machine, planner)
    response = generate_operation_plan(planning_request, client=FakePlannerClient())

    assert response.errors == []
    assert response.plan is not None
    assert response.geometry is not None
    case.op_path.write_text(response.op_yaml, encoding="utf-8")
    job = JobFile.model_validate(response.plan)

    operations = job.model_dump(mode="json", exclude_none=True)["operations"]
    generated_entities = job.model_dump(mode="json", exclude_none=True)["generated_entities"]
    generated_screw_holes = [
        entity for entity in generated_entities if entity["role"] == "screw_hole"
    ]
    generated_clamps = [
        entity for entity in generated_entities if entity["role"] == "clamp"
    ]
    generated_screw_ids = {entity["id"] for entity in generated_screw_holes}
    screw_operations = [
        operation
        for operation in operations
        if operation.get("entity") in generated_screw_ids
    ]
    rough_contours = [
        operation
        for operation in operations
        if operation["type"] == "contour" and operation.get("roughing", {}).get("enabled")
    ]
    finishing_passes = [
        operation
        for operation in operations
        if operation["type"] == "contour"
        and operation.get("finishing", {}).get("enabled")
    ]

    assert geometry.summary.entity_count == assertions["total_entities"]
    assert sum(1 for operation in operations if operation["type"] == "drill") == assertions["drills"]
    if "helical_contours" in assertions:
        assert sum(1 for operation in operations if operation["type"] == "helical_contour") == assertions["helical_contours"]
    if "helical_pockets" in assertions:
        assert sum(1 for operation in operations if operation["type"] == "helical_pocket") == assertions["helical_pockets"]
    assert len(generated_screw_holes) == assertions["generated_screw_holes"]["count"]
    if generated_screw_holes:
        expected_screw_depth = planner.defaults.stock.thickness + planner.defaults.cut_deeper_than_stock
        assert screw_operations
        assert all(operation["type"] == "drill" for operation in screw_operations)
        assert all(operation["depth"] == pytest.approx(expected_screw_depth) for operation in screw_operations)
    assert len(generated_clamps) == assertions["generated_clamps"]["count"]
    assert len(rough_contours) == assertions["rough_contours"]
    assert len(finishing_passes) == assertions["finishing_passes"]
    if planner.defaults.finishing_allowance:
        assert len(rough_contours) == len(finishing_passes)
    assert case.op_path.exists()

    for actual, expected in zip(
        generated_screw_holes,
        assertions["generated_screw_holes"]["locations"],
        strict=True,
    ):
        assert round(actual["center"]["x"]) == expected["x"]
        assert round(actual["center"]["y"]) == expected["y"]
        assert _point_is_in_scrap_area(actual["center"], planning_request)

    for actual, expected in zip(
        generated_clamps,
        assertions["generated_clamps"]["positions"],
        strict=True,
    ):
        assert actual["center"]["x"] == pytest.approx(expected["x"], abs=1e-6)
        assert actual["center"]["y"] == pytest.approx(expected["y"], abs=1e-6)


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_operation_plan_generates_gcode(case):
    ensure_case_outputs(case)
    machine = MachineFile.model_validate(load_yaml_file(case.machine_path))
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    geometry = GeometryFile.model_validate(load_yaml_file(case.geom_path))
    assertions = load_yaml_file(case.gcode_assertions_path)

    planning_request = _planning_request(case, geometry, machine, planner)
    plan_response = generate_operation_plan(planning_request, client=FakePlannerClient())
    assert plan_response.errors == []
    assert plan_response.plan is not None
    assert plan_response.geometry is not None

    toolpath_response = generate_toolpaths(
        ToolpathRequest(
            job=plan_response.plan,
            geometry=plan_response.geometry,
            machine=machine,
            fixed_dxf=case.fixed_path.read_text(encoding="utf-8", errors="ignore"),
        )
    )
    case.gcode_path.write_text(toolpath_response.gcode, encoding="utf-8")

    _assert_toolpath_issues(plan_response.errors, assertions.get("planning_errors", {}), "planning errors")
    _assert_toolpath_issues(plan_response.warnings, assertions.get("planning_warnings", {}), "planning warnings")
    _assert_toolpath_issues(toolpath_response.errors, assertions.get("errors", {}), "errors")
    _assert_toolpath_issues(toolpath_response.warnings, assertions.get("warnings", {}), "warnings")
    assert len(plan_response.errors) <= assertions.get("max_planning_errors", 0)
    assert len(plan_response.warnings) <= assertions.get("max_planning_warnings", 999999)
    assert len(toolpath_response.errors) <= assertions.get("max_errors", 0)
    assert len(toolpath_response.warnings) <= assertions.get("max_warnings", 999999)
    assert case.gcode_path.exists()
    assert "M30" in toolpath_response.gcode
    _assert_gcode(toolpath_response.gcode, assertions.get("gcode", {}))


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_supported_operations_simulate_material_removal(case):
    ensure_case_outputs(case)
    machine = MachineFile.model_validate(load_yaml_file(case.machine_path))
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    geometry = GeometryFile.model_validate(load_yaml_file(case.geom_path))
    assertions = load_yaml_file(case.simulation_assertions_path)

    planning_request = _planning_request(case, geometry, machine, planner)
    plan_response = generate_operation_plan(planning_request, client=FakePlannerClient())
    assert plan_response.errors == []
    assert plan_response.plan is not None
    assert plan_response.geometry is not None
    job = JobFile.model_validate(plan_response.plan)
    simulated_geometry = GeometryFile.model_validate(plan_response.geometry)
    toolpath_response = generate_toolpaths(
        ToolpathRequest(
            job=job,
            geometry=simulated_geometry,
            machine=machine,
            fixed_dxf=case.fixed_path.read_text(encoding="utf-8", errors="ignore"),
        )
    )
    assert toolpath_response.plan is not None
    expected = build_expected_removals(job, simulated_geometry, toolpath_response.plan)
    stock = _simulation_stock(simulated_geometry, planner)
    settings = _simulation_settings(planner)
    simulation_run = simulate_toolpath(
        DexelSimulationRequest(
            job=job,
            machine=machine,
            toolpath_plan=toolpath_response.plan,
            stock=stock,
            settings=settings,
            expected_removals=expected.removals,
        )
    )
    metrics = simulation_run.response.metrics
    contour_geometry = _contour_geometry_report(toolpath_response.plan)
    recut_analysis = _recut_analysis_report(simulation_run.grid)
    summary = {
        "supported_operation_count": len(expected.supported_operation_ids),
        "preview_operation_count": len({toolpath_pass.operation_id for toolpath_pass in toolpath_response.plan.passes}),
        "expected_removal_count": len(expected.removals),
        "expected_builder_warnings": expected.warnings,
        "preview_errors": [issue.model_dump(mode="json") for issue in simulation_run.response.errors],
        "preview_warnings": [issue.model_dump(mode="json") for issue in simulation_run.response.warnings],
        "errors": [issue.model_dump(mode="json") for issue in simulation_run.response.errors],
        "warnings": [issue.model_dump(mode="json") for issue in simulation_run.response.warnings],
        "metrics": metrics.model_dump(mode="json"),
        "contour_geometry": contour_geometry,
        "recut_analysis": recut_analysis,
    }
    dump_yaml_file(case.simulation_summary_path, summary)
    dump_yaml_file(case.recut_summary_path, recut_analysis)
    case.simulation_initial_path.write_bytes(
        render_dexel_preview_png(
            simulation_run.grid,
            f"{case.name} simulation initial",
            depth=simulation_run.grid.actual_depth * 0,
        )
    )
    case.simulation_final_path.write_bytes(
        render_dexel_preview_png(
            simulation_run.grid,
            f"{case.name} simulation final state",
            state_mode="detailed",
            overlay_expected=True,
        )
    )
    _write_contour_error_png(toolpath_response.plan, case.contour_error_path)
    _write_recut_heatmap_png(simulation_run.grid, case.recut_heatmap_path)

    assert len(simulation_run.response.errors) <= assertions.get("max_errors", 0)
    assert len(simulation_run.response.warnings) <= assertions.get("max_warnings", 999999)
    _assert_toolpath_issues(simulation_run.response.errors, assertions.get("errors", {}), "simulation errors")
    _assert_toolpath_issues(simulation_run.response.warnings, assertions.get("warnings", {}), "simulation warnings")
    assert len(expected.supported_operation_ids) == len(job.operations)
    assert not expected.warnings
    assert metrics.rapid_collision_count <= assertions.get("max_rapid_collisions", 0)
    assert metrics.unsafe_rapid_count <= assertions.get("max_unsafe_rapids", 0)
    assert metrics.overcut_cells <= assertions.get("max_overcut_cells", 999999999)
    assert metrics.undercut_cells <= assertions.get("max_undercut_cells", 999999999)
    assert metrics.air_cut_moves <= assertions.get("max_air_cut_moves", 999999999)
    assert metrics.recut_cells <= assertions.get("max_recut_cells", 999999999)
    _assert_contour_geometry(contour_geometry, assertions.get("geometry", {}))
    assert case.simulation_summary_path.exists()
    assert case.simulation_initial_path.stat().st_size > 1000
    assert case.simulation_final_path.stat().st_size > 1000
    assert case.contour_error_path.exists()
    assert case.contour_error_path.stat().st_size > 1000


def test_real_dxf_cleaning_summary_file_is_written():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUTPUT_DIR / "summary.txt"
    lines = [
        "file,source_entities,fixed_entities,closed_loops,open_paths,ignored,zero_length_removed,duplicates_removed,endpoints_snapped,warnings"
    ]

    for case in real_dxf_cases():
        source_count = _entity_count(case.source_path)
        result = clean_dxf(case.source_path, case.fixed_path)
        fixed_count = _entity_count(case.fixed_path)
        lines.append(
            ",".join(
                [
                    case.source_path.name,
                    str(source_count),
                    str(fixed_count),
                    str(result.closed_loops),
                    str(result.open_paths),
                    str(result.entities_ignored),
                    str(result.zero_length_removed),
                    str(result.duplicates_removed),
                    str(result.endpoints_snapped),
                    "|".join(result.warnings),
                ]
            )
        )

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert summary_path.exists()
    assert len(lines) > 1


def test_2xintakev3_geometry_matches_screenshot_expectations():
    case = dxf_case("2xintake")
    ensure_case_outputs(case)
    data = load_yaml_file(case.geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert Counter(entity["shape"] for entity in data["entities"]) == {
        "circle": 50,
        "polyline_with_arcs": 8,
        "rectangle": 1,
    }
    assert data["summary"]["ignored_count"] == 0

    roots = data["entity_map"]
    assert len(roots) == 1
    frame = roots[0]
    assert frame["role"] == "frame"

    parts = frame["children"]
    assert len(parts) == 4
    assert sorted(len(part["children"]) for part in parts) == [5, 5, 22, 22]

    five_hole_parts = [part for part in parts if len(part["children"]) == 5]
    twenty_two_hole_parts = [part for part in parts if len(part["children"]) == 22]

    assert len(five_hole_parts) == 2
    assert len(twenty_two_hole_parts) == 2
    for part in five_hole_parts:
        assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
            "circle": 5
        }

    for part in twenty_two_hole_parts:
        assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
            "circle": 20,
            "polyline_with_arcs": 2,
        }


def test_intake_front_geometry_matches_screenshot_expectations():
    case = dxf_case("intake_frontv2")
    ensure_case_outputs(case)
    data = load_yaml_file(case.geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert Counter(entity["shape"] for entity in data["entities"]) == {
        "circle": 12,
        "polyline_with_arcs": 31,
    }
    assert data["summary"]["ignored_count"] == 0

    roots = data["entity_map"]
    assert len(roots) == 1
    part = roots[0]
    assert part["role"] == "part"
    assert len(part["children"]) == 42
    assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
        "circle": 12,
        "polyline_with_arcs": 30,
    }


def test_intakev4_geometry_matches_screenshot_expectations():
    case = dxf_case("intakev4")
    ensure_case_outputs(case)
    data = load_yaml_file(case.geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert data["units"]["length"] == "in"
    assert data["units"]["source"] == "guessed"

    frames = [node for node in data["entity_map"] if node["role"] == "frame"]
    ignored = [node for node in data["entity_map"] if node["role"] == "ignored"]
    rectangles = [entity for entity in data["entities"] if entity["shape"] == "rectangle"]

    assert len(rectangles) == 5
    assert len(frames) == 1
    assert data["summary"]["ignored_count"] == 4
    assert len(ignored) == 4
    assert all(entities[node["entity"]]["shape"] == "rectangle" for node in ignored)

    parts = frames[0]["children"]
    assert len(parts) == 9

    child_circle_counts = {
        part["entity"]: sum(
            1 for child in part["children"] if entities[child["entity"]]["shape"] == "circle"
        )
        for part in parts
    }
    assert sorted(child_circle_counts.values()) == [2, 2, 3, 3, 3, 5, 5, 15, 17]

    largest_part = max(parts, key=lambda part: _box_area(entities[part["entity"]]["bounding_box"]))
    assert child_circle_counts[largest_part["entity"]] == 17

    circular_parts = [part for part in parts if entities[part["entity"]]["shape"] == "circle"]
    assert len(circular_parts) == 1
    circular_part_children = [
        entities[child["entity"]] for child in circular_parts[0]["children"]
    ]
    assert len(circular_part_children) == 15
    assert sum(1 for child in circular_part_children if child["diameter"] < 0.5) == 14

    high_aspect_parts = [
        part
        for part in parts
        if _box_aspect_ratio(entities[part["entity"]]["bounding_box"]) > 3.0
    ]
    assert sorted(child_circle_counts[part["entity"]] for part in high_aspect_parts) == [2, 2, 3]
    assert sorted(
        count for count in child_circle_counts.values() if count in {3, 5}
    ) == [3, 3, 3, 5, 5]


def test_intakev4_largest_part_has_17_circular_holes():
    case = dxf_case("intakev4")
    ensure_case_outputs(case)
    data = load_yaml_file(case.geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}
    populated_frame = next(
        frame
        for frame in data["entity_map"]
        if frame["role"] == "frame"
    )
    largest_part = max(
        populated_frame["children"],
        key=lambda part: _box_area(entities[part["entity"]]["bounding_box"]),
    )
    assert (
        sum(
            1
            for child in largest_part["children"]
            if entities[child["entity"]]["shape"] == "circle"
        )
        == 17
    )


def _entity_count(path: Path) -> int:
    return len(list(ezdxf.readfile(path).modelspace()))


def _clean_config(planner: PlannerFile | None = None) -> CleanDxfConfig:
    arc_detection = planner.defaults.arc_detection if planner is not None else None
    return CleanDxfConfig(
        gap_tolerance=0.005,
        duplicate_tolerance=0.0005,
        min_segment_length=0.001,
        arc_detection=arc_detection.mode if arc_detection else "OFF",
        arc_tolerance=arc_detection.tolerance if arc_detection else 0.002,
        reorient_to_origin=planner.defaults.origin.reorient_to_origin if planner is not None else False,
    )


def _planning_request(
    case: DxfCase,
    geometry: GeometryFile,
    machine: MachineFile,
    planner: PlannerFile,
) -> PlanningRequest:
    stock = planner.defaults.stock
    assert stock is not None
    return PlanningRequest.model_validate(
        {
            "geometry": geometry.model_dump(mode="json"),
            "machine": machine.model_dump(mode="json"),
            "system_advice": load_system_planner_advice().model_dump(mode="json"),
            "user_advice": planner.operation_advice.model_dump(mode="json"),
            "fixed_dxf": case.fixed_path.read_text(encoding="utf-8", errors="ignore"),
            "inputs": {
                "stock_xy": _stock_size_from_geometry(geometry),
                "stock_units": geometry.units.length,
                "stock_thickness": stock.thickness,
                "stock_material": stock.material,
                "z_zero_position": stock.z_zero,
                "coordinate_system": planner.defaults.coordinate_system,
                "workholding_method": planner.defaults.workholding,
                "tools": planner.defaults.default_tool,
                "cut_deeper_than_stock": planner.defaults.cut_deeper_than_stock,
                "finishing_allowance": planner.defaults.finishing_allowance,
                "screw_spacing": planner.defaults.screw_spacing,
                "ideal_screw_distance": planner.defaults.ideal_screw_distance,
                "min_screw_distance": planner.defaults.min_screw_distance,
                "operation_settings": planner.defaults.operation_settings.model_dump(mode="json"),
                "fixups": _fixup_values(planner),
            },
        }
    )


class FakePlannerClient:
    def generate(self, request: PlanningRequest) -> PlanningResponse:
        from dxfwiz.planning.service import _build_plan, _dump_yaml, _geometry_with_generated_entities

        plan = _build_plan(request, [])
        job = JobFile.model_validate(plan)
        plan_data = job.model_dump(mode="json", exclude_none=True)
        return PlanningResponse(
            errors=[],
            warnings=[],
            geometry=_geometry_with_generated_entities(request.geometry, plan_data),
            plan=plan_data,
            op_yaml=_dump_yaml(plan_data),
        )


def _stock_size_from_geometry(geometry: GeometryFile) -> str:
    entities = {entity.id: entity for entity in geometry.entities}
    frame = next((node for node in geometry.entity_map if node.role == "frame"), None)
    if frame is not None:
        box = entities[frame.entity].bounding_box
        assert box is not None
        width = box.max.x - box.min.x
        height = box.max.y - box.min.y
        return f"{width:.3f} x {height:.3f} {geometry.units.length} frame"
    box = geometry.summary.bounding_box
    width = box.max.x - box.min.x
    height = box.max.y - box.min.y
    return f"{width:.3f} x {height:.3f} {geometry.units.length} extents"


def _simulation_stock(geometry: GeometryFile, planner: PlannerFile) -> SimulationStock:
    stock = planner.defaults.stock
    assert stock is not None
    min_x, min_y, max_x, max_y = _frame_or_summary_bounds(geometry)
    return SimulationStock(
        bounds=SimulationBounds(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y),
        thickness=stock.thickness,
    )


def _simulation_settings(planner: PlannerFile) -> SimulationSettings:
    simulation = planner.defaults.simulation
    return SimulationSettings(
        xy_spacing=simulation.xy_spacing,
        xy_tool_fraction=simulation.xy_tool_fraction,
        z_spacing=simulation.z_spacing,
        max_grid_cells=simulation.max_grid_cells,
        preview=simulation.preview,
        arc_chord_fraction=simulation.arc_chord_fraction,
    )


def _toolpath_plan_for_operations(plan: ToolpathPlan, operation_ids: set[str]) -> ToolpathPlan:
    return ToolpathPlan(
        units=plan.units,
        coordinate_system=plan.coordinate_system,
        commands=plan.commands,
        source_paths=plan.source_paths,
        passes=[toolpath_pass for toolpath_pass in plan.passes if toolpath_pass.operation_id in operation_ids],
        warnings=plan.warnings,
    )


def _point_is_in_scrap_area(point: dict[str, float], request: PlanningRequest) -> bool:
    min_x, min_y, max_x, max_y = _frame_or_summary_bounds(request.geometry)
    location = Point(point["x"], point["y"])
    if not box(min_x, min_y, max_x, max_y).covers(location):
        return False
    screw_method = next(
        (
            method
            for method in request.machine.machine.workholding.supported_methods
            if getattr(method, "method", None) == "screws"
        ),
        None,
    )
    clearance = (
        getattr(screw_method, "screw_clearance", None)
        or request.machine.machine.screw_clearance
        or 0.0
    )
    blocked = _part_clearance_polygons(request, clearance)
    return blocked is None or blocked.is_empty or not blocked.covers(location)


def _box_contains_point(box, point: dict[str, float]) -> bool:
    return (
        box.min.x <= point["x"] <= box.max.x
        and box.min.y <= point["y"] <= box.max.y
    )


def _box_area(box: dict) -> float:
    return (box["max"]["x"] - box["min"]["x"]) * (box["max"]["y"] - box["min"]["y"])


def _box_aspect_ratio(box: dict) -> float:
    width = box["max"]["x"] - box["min"]["x"]
    height = box["max"]["y"] - box["min"]["y"]
    return max(width, height) / min(width, height)


def _has_role(nodes: list[dict], role: str) -> bool:
    return any(
        node["role"] == role or _has_role(node.get("children", []), role)
        for node in nodes
    )


def _assert_toolpath_issues(issues, assertions: dict, label: str) -> None:
    codes = [issue.code for issue in issues]
    for code in assertions.get("has_codes", []):
        assert code in codes, f"Expected {label} to include {code}, got {codes}"
    for code in assertions.get("does_not_have_codes", []):
        assert code not in codes, f"Expected {label} to exclude {code}, got {codes}"
    max_severity = assertions.get("max_severity")
    if max_severity is not None:
        assert all(_issue_severity(code) <= max_severity for code in codes), codes


def _assert_gcode(gcode: str, assertions: dict) -> None:
    for text in assertions.get("has_substrings", []):
        assert text in gcode, f"Expected G-code to include {text!r}"
    for text in assertions.get("does_not_have_substrings", []):
        assert text not in gcode, f"Expected G-code to exclude {text!r}"
    for item in assertions.get("min_substring_counts", []):
        text = item["text"]
        assert gcode.count(text) >= item["count"], (
            f"Expected G-code to include {text!r} at least {item['count']} times, "
            f"got {gcode.count(text)}"
        )


def _recut_analysis_report(grid, top_n: int = 12) -> dict:
    cut_count = grid.cut_count
    recut_mask = cut_count > 1
    excessive_mask = cut_count > 2
    operation_rows = []
    for operation_index in sorted(int(index) for index in np.unique(grid.last_operation_id[recut_mask]) if index >= 0):
        mask = recut_mask & (grid.last_operation_id == operation_index)
        ys, xs = np.where(mask)
        if xs.size == 0:
            continue
        operation_rows.append(
            {
                "operation_id": grid.operation_lookup.get(operation_index, f"unknown-{operation_index}"),
                "recut_cells": int(xs.size),
                "excessive_recut_cells": int(np.count_nonzero(excessive_mask & (grid.last_operation_id == operation_index))),
                "max_cut_count": int(cut_count[mask].max(initial=0)),
                "bbox": {
                    "min": {"x": float(grid.x_values[xs].min()), "y": float(grid.y_values[ys].min())},
                    "max": {"x": float(grid.x_values[xs].max()), "y": float(grid.y_values[ys].max())},
                },
            }
        )
    operation_rows.sort(key=lambda row: (row["recut_cells"], row["max_cut_count"]), reverse=True)
    return {
        "note": "cut_count excludes touches after a cell is already cut through the stock thickness",
        "recut_cells": int(np.count_nonzero(recut_mask)),
        "excessive_recut_cells": int(np.count_nonzero(excessive_mask)),
        "max_cut_count": int(cut_count.max(initial=0)),
        "top_last_operations": operation_rows[:top_n],
    }


def _write_recut_heatmap_png(grid, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    heat = np.where(grid.cut_count > 1, grid.cut_count, np.nan)
    fig, ax = plt.subplots(figsize=(8, 6), dpi=160)
    extent = [
        float(grid.x_values[0] - grid.xy_spacing / 2),
        float(grid.x_values[-1] + grid.xy_spacing / 2),
        float(grid.y_values[0] - grid.xy_spacing / 2),
        float(grid.y_values[-1] + grid.xy_spacing / 2),
    ]
    image = ax.imshow(heat, origin="lower", extent=extent, cmap="magma", interpolation="nearest")
    if np.isfinite(heat).any():
        colorbar = fig.colorbar(image, ax=ax, shrink=0.8)
        colorbar.set_label("cut count")
    ax.set_title("Recut cells (cut_count > 1)")
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, color="#e5e7eb", linewidth=0.25, alpha=0.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, format="png", bbox_inches="tight")
    plt.close(fig)


def _contour_geometry_report(plan: ToolpathPlan) -> list[dict]:
    source_paths = {source_path.id: source_path for source_path in plan.source_paths}
    report = []
    for toolpath_pass in plan.passes:
        if toolpath_pass.kind not in {"rough_contour", "finish_contour"}:
            continue
        if toolpath_pass.source_path is None or toolpath_pass.offset_distance is None:
            continue
        source_path = source_paths.get(toolpath_pass.source_path)
        if source_path is None:
            continue
        stats = contour_offset_validation(source_path, toolpath_pass)
        report.append(
            {
                "pass_id": toolpath_pass.id,
                "operation_id": toolpath_pass.operation_id,
                "entity": toolpath_pass.entity,
                "kind": toolpath_pass.kind,
                "offset_side": toolpath_pass.offset_side,
                "offset_distance": toolpath_pass.offset_distance,
                "stats": stats,
            }
        )
    return report


def _write_contour_error_png(
    plan: ToolpathPlan,
    path: Path,
    ignore_below: float = 0.002,
    problem_above: float = 0.005,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    source_paths = {source_path.id: source_path for source_path in plan.source_paths}
    warning_points = []
    problem_points = []
    for toolpath_pass in plan.passes:
        if toolpath_pass.kind not in {"rough_contour", "finish_contour"}:
            continue
        if toolpath_pass.source_path is None or toolpath_pass.offset_distance is None:
            continue
        source_path = source_paths.get(toolpath_pass.source_path)
        if source_path is None:
            continue
        for sample in contour_offset_error_samples(source_path, toolpath_pass):
            if sample["error"] < ignore_below:
                continue
            if sample["error"] > problem_above:
                problem_points.append(sample)
            else:
                warning_points.append(sample)

    fig, ax = plt.subplots(figsize=(8, 6), dpi=160)
    for source_path in plan.source_paths:
        if not source_path.closed:
            continue
        points = source_path_points(source_path)
        if len(points) < 2:
            continue
        xs = [point[0] for point in [*points, points[0]]]
        ys = [point[1] for point in [*points, points[0]]]
        ax.plot(xs, ys, color="#94a3b8", linewidth=0.7, alpha=0.45)

    if warning_points:
        ax.scatter(
            [point["x"] for point in warning_points],
            [point["y"] for point in warning_points],
            c="#f59e0b",
            s=8,
            label=f"{ignore_below:.3f}-{problem_above:.3f} in",
            alpha=0.8,
            linewidths=0,
        )
    if problem_points:
        ax.scatter(
            [point["x"] for point in problem_points],
            [point["y"] for point in problem_points],
            c="#dc2626",
            s=12,
            label=f"> {problem_above:.3f} in",
            alpha=0.9,
            linewidths=0,
        )

    worst = sorted(problem_points, key=lambda point: point["error"], reverse=True)[:8]
    for point in worst:
        ax.annotate(
            f"{point['pass_id']} {point['error']:.3f}",
            (point["x"], point["y"]),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=6,
            color="#7f1d1d",
        )

    ax.set_title("Contour offset error samples")
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, color="#e5e7eb", linewidth=0.4)
    subtitle = f"Ignoring < {ignore_below:.3f} in; red marks > {problem_above:.3f} in"
    ax.text(0.01, 0.01, subtitle, transform=ax.transAxes, fontsize=8, color="#475569")
    if warning_points or problem_points:
        ax.legend(loc="upper right", fontsize=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, format="png", bbox_inches="tight")
    plt.close(fig)


def _assert_contour_geometry(report: list[dict], assertions: dict) -> None:
    if not assertions:
        return
    max_median_error = assertions.get("max_contour_offset_median_error")
    max_error = assertions.get("max_contour_offset_max_error")
    max_coverage_error = assertions.get("max_contour_offset_coverage_error", max_error)
    if max_median_error is None and max_error is None and max_coverage_error is None:
        return
    failures = []
    for item in report:
        stats = item["stats"]
        if not stats:
            failures.append(f"{item['pass_id']} could not compute contour offset validation")
            continue
        median_error = stats["actual_to_expected_median"]
        pass_max_error = stats["actual_to_expected_max"]
        coverage_error = stats["expected_to_actual_max"]
        if max_median_error is not None and median_error > max_median_error:
            failures.append(
                f"{item['pass_id']} median offset error {median_error:.6f} "
                f"> {max_median_error:.6f} (stats={stats})"
            )
        if max_error is not None and pass_max_error > max_error:
            failures.append(
                f"{item['pass_id']} max offset error {pass_max_error:.6f} "
                f"> {max_error:.6f} (stats={stats})"
            )
        if max_coverage_error is not None and coverage_error > max_coverage_error:
            failures.append(
                f"{item['pass_id']} expected offset coverage error {coverage_error:.6f} "
                f"> {max_coverage_error:.6f} (stats={stats})"
            )
    assert not failures, "\n".join(failures)


def _issue_severity(code: str) -> int:
    return int(code[1]) if len(code) > 1 and code[1].isdigit() else 0


def _fixup_values(planner: PlannerFile) -> dict[str, bool]:
    return {
        name: bool(setting["enabled"])
        for name, setting in planner.defaults.fixups.model_dump().items()
    }
