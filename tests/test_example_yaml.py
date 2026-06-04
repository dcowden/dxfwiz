from pathlib import Path

import pytest

from dxfwiz.schemas import (
    GeometryFile,
    JobFile,
    MachineFile,
    OperationInputsFile,
    PlannerFile,
    PostFile,
)
from dxfwiz.yaml_io import load_yaml_file


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("machine.yaml", MachineFile),
        ("post.yaml", PostFile),
        ("planner.yaml", PlannerFile),
        ("geom.yaml", GeometryFile),
        ("job.yaml", JobFile),
        ("operation_inputs.yaml", OperationInputsFile),
    ],
)
def test_example_yaml_validates(filename, model):
    data = load_yaml_file(EXAMPLES / filename)
    model.model_validate(data)


def test_example_job_references_existing_bundle_members():
    machine = MachineFile.model_validate(load_yaml_file(EXAMPLES / "machine.yaml"))
    geom = GeometryFile.model_validate(load_yaml_file(EXAMPLES / "geom.yaml"))
    job = JobFile.model_validate(load_yaml_file(EXAMPLES / "job.yaml"))

    entity_ids = {entity.id for entity in geom.entities}
    tool_ids = {tool.id for tool in machine.tools}

    assert job.job.machine == "machine.yaml"
    assert job.job.post == "post.yaml"
    assert job.job.planner == "planner.yaml"
    assert job.job.geometry_file == "bearing_block_geom.yaml"
    assert {operation.entity for operation in job.operations} <= entity_ids
    assert {operation.tool for operation in job.operations} <= tool_ids


def test_example_planner_uses_machine_holding_options():
    machine = MachineFile.model_validate(load_yaml_file(EXAMPLES / "machine.yaml"))
    planner = PlannerFile.model_validate(load_yaml_file(EXAMPLES / "planner.yaml"))

    assert set(planner.defaults.workholding) <= set(machine.machine.workholding)
    assert set(planner.defaults.part_holding) <= set(machine.machine.part_holding)


def test_example_planner_max_tools_is_planner_preference():
    machine = MachineFile.model_validate(load_yaml_file(EXAMPLES / "machine.yaml"))
    planner = PlannerFile.model_validate(load_yaml_file(EXAMPLES / "planner.yaml"))

    assert machine.machine.max_tools == 1
    assert planner.defaults.max_tools == 1


def test_example_planner_replaces_auto_rules_with_geometry_advice():
    planner = PlannerFile.model_validate(load_yaml_file(EXAMPLES / "planner.yaml"))

    assert any("prefer a drill operation" in item for item in planner.operation_advice.geometry)
    assert any("prefer a helical_drill operation" in item for item in planner.operation_advice.geometry)


def test_operation_inputs_are_fixed_required_checklist():
    operation_inputs = OperationInputsFile.model_validate(
        load_yaml_file(EXAMPLES / "operation_inputs.yaml")
    )

    assert [item.name for item in operation_inputs.inputs] == [
        "stock_size",
        "stock_material",
        "workholding_method",
        "z_zero_position",
        "coordinate_system",
    ]
    assert all(item.required for item in operation_inputs.inputs)
