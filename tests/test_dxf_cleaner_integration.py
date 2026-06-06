from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf
import pytest

from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml
from dxfwiz.planning import PlanningRequest, generate_operation_plan, load_system_planner_advice
from dxfwiz.planning.service import PlanningResponse
from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.schemas.job import JobFile
from dxfwiz.svg import render_geometry_svg
from dxfwiz.yaml_io import load_yaml_file


INPUT_DIR = Path(__file__).resolve().parent / "dxf_clean"
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
    fixed_path: Path
    geom_path: Path
    svg_path: Path
    op_path: Path


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
                fixed_path=output_dir / f"{source_path.stem}_fixed.dxf",
                geom_path=output_dir / f"{source_path.stem}_geom.yaml",
                svg_path=output_dir / f"{source_path.stem}_geometry.svg",
                op_path=output_dir / f"{source_path.stem}_op.yaml",
            )
        )
    return cases


def dxf_case(name: str) -> DxfCase:
    return next(case for case in real_dxf_cases() if case.name == name)


def ensure_case_outputs(case: DxfCase) -> None:
    case.output_dir.mkdir(parents=True, exist_ok=True)
    if not case.fixed_path.exists():
        clean_dxf(case.source_path, case.fixed_path, _clean_config())
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
        _clean_config(),
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
    if case.name != "intake_frontv2":
        assert "role-frame" in svg


@pytest.mark.parametrize("case", real_dxf_cases(), ids=lambda case: case.name)
def test_real_dxf_operation_plan_outputs_op_yaml(case):
    ensure_case_outputs(case)
    machine = MachineFile.model_validate(load_yaml_file(case.machine_path))
    planner = PlannerFile.model_validate(load_yaml_file(case.planner_path))
    geometry = GeometryFile.model_validate(load_yaml_file(case.geom_path))
    assertions = load_yaml_file(case.operation_assertions_path)

    response = generate_operation_plan(
        _planning_request(case, geometry, machine, planner),
        client=FakePlannerClient(),
    )

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
    rough_contours = [
        operation
        for operation in operations
        if operation["type"] == "contour" and operation.get("tabs", {}).get("enabled")
    ]
    finishing_passes = [
        operation
        for operation in operations
        if operation["type"] == "contour"
        and operation.get("finishing_pass", {}).get("enabled")
    ]

    assert geometry.summary.entity_count == assertions["total_entities"]
    assert sum(1 for operation in operations if operation["type"] == "drill") == assertions["drills"]
    assert sum(1 for operation in operations if operation["type"] == "helical_drill") == assertions["helical_drills"]
    assert len(generated_screw_holes) == assertions["generated_screw_holes"]["count"]
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
        assert _point_is_in_scrap_area(actual["center"], geometry)

    for actual, expected in zip(
        generated_clamps,
        assertions["generated_clamps"]["positions"],
        strict=True,
    ):
        assert actual["center"]["x"] == pytest.approx(expected["x"], abs=1e-6)
        assert actual["center"]["y"] == pytest.approx(expected["y"], abs=1e-6)


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


def _clean_config() -> CleanDxfConfig:
    return CleanDxfConfig(
        gap_tolerance=0.005,
        duplicate_tolerance=0.0005,
        min_segment_length=0.001,
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


def _point_is_in_scrap_area(point: dict[str, float], geometry: GeometryFile) -> bool:
    entities = {entity.id: entity for entity in geometry.entities}
    frame = next((node for node in geometry.entity_map if node.role == "frame"), None)
    if frame is None:
        return True
    frame_box = entities[frame.entity].bounding_box
    assert frame_box is not None
    if not _box_contains_point(frame_box, point):
        return False
    for part in frame.children:
        if part.role != "part":
            continue
        part_box = entities[part.entity].bounding_box
        if part_box is not None and _box_contains_point(part_box, point):
            return False
    return True


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
