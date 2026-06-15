import pytest

from dxfwiz.cam_kernel import cavalier
from dxfwiz.schemas import GeometryFile, JobFile, MachineFile
from dxfwiz.toolpaths.core import compile_toolpath_plan
from dxfwiz.yaml_io import load_yaml_file


def _fixture_geometry() -> GeometryFile:
    return GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {
                "length": "in",
                "source": "guessed",
                "confidence": 0.8,
                "coordinate_scale": 1.0,
            },
            "source": {"original_file": "test.dxf", "cleaned_file": "test_fixed.dxf", "format": "dxf"},
            "summary": {
                "entity_count": 1,
                "closed_count": 1,
                "open_count": 0,
                "ignored_count": 0,
                "generated_count": 0,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 2, "y": 1}},
            },
            "entity_map": [{"entity": "pocket1", "role": "cutout", "children": []}],
            "entities": [
                {
                    "id": "pocket1",
                    "type": "closed_loop",
                    "shape": "rectangle",
                    "source_refs": [],
                    "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 2, "y": 1}},
                }
            ],
        }
    )


def _fixture_job() -> JobFile:
    return JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "engine toggle",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "post": "post.yaml",
                "planner": "planner.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "operations": [
                {
                    "id": "op-pocket",
                    "type": "pocket",
                    "entity": "pocket1",
                    "tool": "t5",
                    "depth": 0.1,
                    "strategy": "offset",
                    "stepover_percent": 50,
                    "roughing": {
                        "enabled": True,
                        "depth_per_pass": 0.1,
                        "side_allowance": 0.0,
                        "bottom_allowance": 0.0,
                        "milling_direction": "climb",
                    },
                    "finishing": {"enabled": False},
                }
            ],
        }
    )


def _fixture_machine(toolpath_engine: str | None = None) -> MachineFile:
    machine_data = load_yaml_file("examples/machine.yaml")
    if toolpath_engine is not None:
        machine_data["machine"]["toolpath_engine"] = toolpath_engine
    return MachineFile.model_validate(machine_data)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_compile_toolpath_plan_defaults_to_cavalier_and_can_toggle_to_legacy():
    geometry = _fixture_geometry()
    job = _fixture_job()

    cavalier_plan = compile_toolpath_plan(job, geometry, _fixture_machine())
    legacy_plan = compile_toolpath_plan(job, geometry, _fixture_machine("legacy"))

    assert any("-cavc-" in toolpath_pass.id for toolpath_pass in cavalier_plan.passes)
    assert not any("-cavc-" in toolpath_pass.id for toolpath_pass in legacy_plan.passes)
    assert not any("Cavalier toolpath generation failed" in warning for warning in cavalier_plan.warnings)
    assert not any("Cavalier toolpath generation failed" in warning for warning in legacy_plan.warnings)
