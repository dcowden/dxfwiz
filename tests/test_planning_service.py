from pathlib import Path

from dxfwiz.dxf import clean_dxf, write_geometry_yaml
from dxfwiz.planning import PlanningRequest, generate_operation_plan, load_system_planner_advice
from dxfwiz.planning.service import PlanningResponse
from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.schemas.job import JobFile
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
    assert job.tools[0].tool == "t5"
    assert job.tools[0].diameter == 0.25


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


def _planning_request(tmp_path, inputs):
    source = ROOT / "tests" / "dxf_clean" / "2xintake" / "2xintakev3_and_2xkickerv1.dxf"
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
            "inputs": inputs,
        }
    )
