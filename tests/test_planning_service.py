from pathlib import Path

from dxfwiz.dxf import clean_dxf, write_geometry_yaml
from dxfwiz.planning import PlanningRequest, generate_operation_plan, load_system_planner_advice
from dxfwiz.planning.service import PlanningResponse, _hole_operation_type
from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.schemas.job import JobFile
from dxfwiz.toolpaths import ToolpathRequest, generate_toolpaths
from dxfwiz.yaml_io import load_yaml_file


ROOT = Path(__file__).resolve().parents[1]


def test_planner_returns_errors_for_missing_required_inputs(tmp_path):
    request = _planning_request(tmp_path, inputs={"stock_xy": "10 x 10 in"})

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.plan is None
    assert response.op_yaml == ""
    assert {error.field for error in response.errors} >= {
        "stock_thickness",
        "stock_material",
        "z_zero_position",
        "coordinate_system",
        "workholding_method",
    }


def test_system_planner_advice_yaml_loads():
    advice = load_system_planner_advice()

    assert advice.strategies
    assert advice.workholding


def test_ai_prompt_uses_geom_without_raw_dxf(tmp_path):
    from dxfwiz.planning.ai import _planner_prompt

    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "tools": "t5",
        },
    )
    request.fixed_dxf = "RAW_DXF_SENTINEL_SHOULD_NOT_APPEAR"

    prompt = _planner_prompt(request)

    assert "GEOM_YAML:" in prompt
    assert "entities:" in prompt
    assert "RAW_DXF_SENTINEL_SHOULD_NOT_APPEAR" not in prompt
    assert "FIXED_DXF:" not in prompt
    assert "Do not create separate finish operations" in prompt
    assert "roughing.side_allowance" in prompt


def test_planner_calls_ai_client_for_operation_plan(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "tools": "t5",
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )
    client = FakePlannerClient()

    response = generate_operation_plan(request, client=client)

    assert client.called
    assert response.errors == []
    assert response.plan is not None
    assert response.geometry is not None
    GeometryFile.model_validate(response.geometry)
    assert response.geometry["summary"]["generated_count"] == len(response.geometry["generated_entities"])
    assert any(entity["id"].startswith("wh") for entity in response.geometry["generated_entities"])
    assert "operations:" in response.op_yaml
    job = JobFile.model_validate(response.plan)
    assert job.stock.material == "plywood"
    assert job.coordinate_system == "G55"
    assert {operation.type for operation in job.operations} >= {"contour", "drill"}
    assert job.tools[0].tool == "t3"
    assert job.tools[0].diameter == 0.1875


def test_ai_plan_missing_contour_finishing_settings_is_repaired(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "tools": "t5",
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )
    response = generate_operation_plan(request, client=MissingFinishPlannerClient())

    assert response.errors == []
    assert any(warning.code == "W2006" for warning in response.warnings)
    job = JobFile.model_validate(response.plan)
    contour_operations = [
        operation
        for operation in job.operations
        if operation.type == "contour"
        and operation.offset == "outside"
    ]
    assert contour_operations
    assert all(operation.finishing.enabled for operation in contour_operations)
    assert all(operation.roughing.side_allowance == 0.01 for operation in contour_operations)
    assert all(operation.ramping for operation in contour_operations)
    assert "contours" in {group.name for group in job.operation_groups}


def test_local_planner_ramps_outer_contours(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.errors == []
    job = JobFile.model_validate(response.plan)
    outer_contours = [
        operation
        for operation in job.operations
        if operation.type == "contour" and operation.offset == "outside"
    ]
    assert outer_contours
    assert all(operation.ramping for operation in outer_contours)


def test_local_planner_selects_largest_single_tool_when_no_tool_is_specified(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.errors == []
    job = JobFile.model_validate(response.plan)
    assert job.tools[0].tool == "t3"
    assert all(getattr(operation, "tool", None) == "t3" for operation in job.operations)


def test_local_planner_uses_helical_pockets_for_large_round_holes(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.errors == []
    job = JobFile.model_validate(response.plan)
    assert sum(1 for operation in job.operations if operation.type == "drill" and not operation.entity.startswith("wh")) == 36
    assert sum(1 for operation in job.operations if operation.type == "helical_pocket") == 14
    assert sum(1 for operation in job.operations if operation.type == "helical_contour") == 0


def test_local_planner_oversizes_screw_holes_to_avoid_toolchange_for_single_tool_machine(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.errors == []
    job = JobFile.model_validate(response.plan)
    assert [tool.tool for tool in job.tools] == ["t3"]
    assert all(getattr(operation, "tool", None) == "t3" for operation in job.operations)
    screw_holes = [entity for entity in job.generated_entities if entity.role == "screw_hole"]
    screw_ops = [operation for operation in job.operations if getattr(operation, "entity", "").startswith("wh")]
    assert screw_holes
    assert {round(entity.diameter, 4) for entity in screw_holes} == {0.1875}
    assert screw_ops
    assert {round(operation.depth, 4) for operation in screw_ops} == {0.26}
    assert {round(operation.peck_depth, 4) for operation in screw_ops} == {0.0938}


def test_local_planner_uses_screw_tool_and_split_nc_files_when_oversizing_disabled(tmp_path):
    request = _planning_request(
        tmp_path,
        inputs={
            "stock_xy": "9.500 x 48.000 in frame",
            "stock_units": "in",
            "stock_thickness": 0.25,
            "stock_material": "plywood",
            "z_zero_position": "stock_top",
            "coordinate_system": "G55",
            "workholding_method": ["screws"],
            "cut_deeper_than_stock": 0.01,
            "finishing_allowance": 0.01,
        },
    )
    request = _with_screw_oversizing(request, False)

    response = generate_operation_plan(request, client=FakePlannerClient())

    assert response.errors == []
    job = JobFile.model_validate(response.plan)
    assert [tool.tool for tool in job.tools] == ["t1", "t3"]
    screw_ops = [operation for operation in job.operations if operation.entity.startswith("wh")]
    feature_ops = [operation for operation in job.operations if not operation.entity.startswith("wh")]
    assert screw_ops
    assert feature_ops
    assert {operation.tool for operation in screw_ops} == {"t1"}
    assert {operation.tool for operation in feature_ops} == {"t3"}

    toolpath_response = generate_toolpaths(
        ToolpathRequest(
            job=job,
            geometry=GeometryFile.model_validate(response.geometry),
            machine=request.machine,
            fixed_dxf=request.fixed_dxf,
        )
    )

    assert toolpath_response.errors == []
    assert set(toolpath_response.gcode_files) == {
        "Generated_Operation_Plan_t1.nc",
        "Generated_Operation_Plan_t3.nc",
    }
    assert "(op1: Pre-drill screw location wh1)" in toolpath_response.gcode_files["Generated_Operation_Plan_t1.nc"]
    assert "T1 M6" in toolpath_response.gcode_files["Generated_Operation_Plan_t1.nc"]
    assert "T3 M6" in toolpath_response.gcode_files["Generated_Operation_Plan_t3.nc"]


def test_local_planner_hole_decision_uses_drill_threshold():
    assert _hole_operation_type(
        hole_diameter=0.201,
        tool_diameter=0.1875,
        drill_max_diameter=0.21,
        max_plug_diameter=0.25,
        helical_pocket_max_diameter=2.0,
    ) == "drill"


def test_local_planner_hole_decision_uses_helical_contour_for_small_plug():
    assert _hole_operation_type(
        hole_diameter=0.55,
        tool_diameter=0.1875,
        drill_max_diameter=0.21,
        max_plug_diameter=0.25,
        helical_pocket_max_diameter=2.0,
    ) == "helical_contour"


def test_local_planner_hole_decision_uses_helical_pocket_for_large_plug():
    assert _hole_operation_type(
        hole_diameter=1.25,
        tool_diameter=0.1875,
        drill_max_diameter=0.21,
        max_plug_diameter=0.25,
        helical_pocket_max_diameter=2.0,
    ) == "helical_pocket"


def test_local_planner_hole_decision_uses_general_pocket_above_helical_limit():
    assert _hole_operation_type(
        hole_diameter=2.25,
        tool_diameter=0.1875,
        drill_max_diameter=0.21,
        max_plug_diameter=0.25,
        helical_pocket_max_diameter=2.0,
    ) == "pocket"


class FakePlannerClient:
    def __init__(self) -> None:
        self.called = False

    def generate(self, request: PlanningRequest) -> PlanningResponse:
        self.called = True
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


class MissingFinishPlannerClient(FakePlannerClient):
    def generate(self, request: PlanningRequest) -> PlanningResponse:
        response = super().generate(request)
        for operation in response.plan["operations"]:
            if operation.get("type") == "contour":
                operation["roughing"]["side_allowance"] = 0.0
                operation["finishing"]["enabled"] = False
        response.op_yaml = ""
        return response


def _planning_request(tmp_path, inputs):
    source = ROOT / "tests" / "integration_tests" / "2xintake" / "2xintakev3_and_2xkickerv1.dxf"
    fixed = tmp_path / "fixed.dxf"
    geom_path = tmp_path / "geom.yaml"
    clean_dxf(source, fixed)
    geometry_data = write_geometry_yaml(
        fixed,
        geom_path,
        original_file=source.name,
        cleaned_file=fixed.name,
    )
    machine = MachineFile.model_validate(load_yaml_file(ROOT / "examples" / "machine.yaml"))
    planner = PlannerFile.model_validate(load_yaml_file(ROOT / "examples" / "planner.yaml"))
    return PlanningRequest.model_validate(
        {
            "geometry": GeometryFile.model_validate(geometry_data).model_dump(mode="json"),
            "machine": machine.model_dump(mode="json"),
            "system_advice": load_system_planner_advice().model_dump(mode="json"),
            "user_advice": planner.operation_advice.model_dump(mode="json"),
            "fixed_dxf": fixed.read_text(encoding="utf-8", errors="ignore"),
            "inputs": {
                **inputs,
                "fixups": {
                    name: bool(setting["enabled"])
                    for name, setting in planner.defaults.fixups.model_dump().items()
                },
                "ideal_screw_distance": planner.defaults.ideal_screw_distance,
                "operation_settings": planner.defaults.operation_settings.model_dump(mode="json"),
            },
        }
    )


def _with_screw_oversizing(request: PlanningRequest, enabled: bool) -> PlanningRequest:
    data = request.model_dump(mode="json")
    for method in data["machine"]["machine"]["workholding"]["supported_methods"]:
        if method["method"] == "screws":
            method["allow_oversized_holes_to_prevent_toolchange"] = enabled
    return PlanningRequest.model_validate(data)
