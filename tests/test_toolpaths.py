from dxfwiz.schemas import GeometryFile, JobFile, MachineFile
from dxfwiz.toolpaths import ToolpathRequest, generate_toolpaths
from dxfwiz.yaml_io import load_yaml_file


def test_uccnc_toolpaths_include_setup_drill_helix_and_contour():
    machine = MachineFile.model_validate(load_yaml_file("examples/machine.yaml"))
    geometry = GeometryFile.model_validate(
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
                "entity_count": 2,
                "closed_count": 2,
                "open_count": 0,
                "ignored_count": 0,
                "generated_count": 1,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 4, "y": 3}},
            },
            "entity_map": [
                {"entity": "e2", "role": "part", "children": [{"entity": "e1", "role": "cutout"}]}
            ],
            "entities": [
                {
                    "id": "e1",
                    "type": "closed_loop",
                    "shape": "circle",
                    "source_refs": [],
                    "center": {"x": 1.5, "y": 1.5},
                    "diameter": 0.5,
                    "bounding_box": {"min": {"x": 1.25, "y": 1.25}, "max": {"x": 1.75, "y": 1.75}},
                },
                {
                    "id": "e2",
                    "type": "closed_loop",
                    "shape": "polyline",
                    "source_refs": [],
                    "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 4, "y": 3}},
                },
            ],
        }
    )
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "test",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "post": "post.yaml",
                "planner": "planner.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "generated_entities": [
                {
                    "id": "wh1",
                    "role": "screw_hole",
                    "shape": "circle",
                    "center": {"x": 0.25, "y": 0.25},
                    "diameter": 0.125,
                }
            ],
            "operations": [
                {
                    "id": "op1",
                    "type": "drill",
                    "entity": "wh1",
                    "tool": "t5",
                    "depth": 0.2,
                    "peck_depth": 0.1,
                    "retract_amount": 0.04,
                },
                {
                    "id": "op2",
                    "type": "helical_drill",
                    "entity": "e1",
                    "tool": "t5",
                    "depth": 0.26,
                    "pitch": 0.08,
                    "milling_direction": "climb",
                    "finishing": {"enabled": True, "side": True, "bottom": False},
                },
                {
                    "id": "op3",
                    "type": "contour",
                    "entity": "e2",
                    "tool": "t5",
                    "depth": 0.25,
                    "extra_depth": 0.01,
                    "offset": "outside",
                    "roughing": {
                        "enabled": True,
                        "depth_per_pass": 0.08,
                        "side_allowance": 0.0,
                        "bottom_allowance": 0.0,
                        "milling_direction": "climb",
                    },
                    "finishing": {"enabled": False},
                    "tabs": {
                        "enabled": True,
                        "width": 1.0,
                        "height": 0.1,
                        "count": 1,
                        "locations": [
                            {
                                "center": {"x": 2.0, "y": -0.125},
                                "lower_left": {"x": 1.5, "y": -0.3},
                                "upper_right": {"x": 2.5, "y": 0.05},
                                "width": 1.0,
                                "height": 0.1,
                                "angle_deg": 0.0,
                            }
                        ],
                    },
                },
            ],
        }
    )

    response = generate_toolpaths(ToolpathRequest(job=job, geometry=geometry, machine=machine))

    assert response.errors == []
    assert "G20" in response.gcode
    assert "G90" in response.gcode
    assert "G55" in response.gcode
    assert "S18000" in response.gcode
    assert "M3" in response.gcode
    assert "G1 Z-0.1" in response.gcode
    assert "G3" in response.gcode
    assert "(op3:" in response.gcode
    assert "G1 Z-0.15" in response.gcode
    assert "tab lifting is not yet implemented" not in response.gcode
    assert "M30" in response.gcode


def test_uccnc_toolpaths_sort_operations_by_tool_before_group_by_default():
    machine = MachineFile.model_validate(load_yaml_file("examples/machine.yaml"))
    geometry = _fixture_geometry()
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "sort test",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "post": "post.yaml",
                "planner": "planner.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "generated_entities": [
                {"id": "wh1", "role": "screw_hole", "shape": "circle", "center": {"x": 0, "y": 0}, "diameter": 0.125},
                {"id": "wh2", "role": "screw_hole", "shape": "circle", "center": {"x": 10, "y": 0}, "diameter": 0.125},
                {"id": "wh3", "role": "screw_hole", "shape": "circle", "center": {"x": 1, "y": 0}, "diameter": 0.125},
                {"id": "wh4", "role": "screw_hole", "shape": "circle", "center": {"x": 11, "y": 0}, "diameter": 0.125},
            ],
            "operation_groups": [
                {"name": "fixtures", "operations": ["op-fixture-small", "op-fixture-large"]},
                {"name": "contours", "operations": ["op-contour-small", "op-contour-large"]},
            ],
            "operations": [
                _drill_op("op-fixture-small", "wh1", "t1"),
                _drill_op("op-fixture-large", "wh2", "t5"),
                _drill_op("op-contour-small", "wh3", "t1"),
                _drill_op("op-contour-large", "wh4", "t5"),
            ],
        }
    )

    response = generate_toolpaths(ToolpathRequest(job=job, geometry=geometry, machine=machine))

    assert response.errors == []
    assert _line_index(response.gcode, "(op-fixture-small:") < _line_index(response.gcode, "(op-contour-small:")
    assert _line_index(response.gcode, "(op-contour-small:") < _line_index(response.gcode, "(op-fixture-large:")
    assert _line_index(response.gcode, "(op-fixture-large:") < _line_index(response.gcode, "(op-contour-large:")


def _fixture_geometry() -> GeometryFile:
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
                "generated_count": 4,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 12, "y": 1}},
            },
            "entity_map": [],
            "entities": [],
        }
    )


def _drill_op(operation_id: str, entity_id: str, tool_id: str) -> dict:
    return {
        "id": operation_id,
        "type": "drill",
        "entity": entity_id,
        "tool": tool_id,
        "depth": 0.2,
        "peck_depth": 0.1,
        "retract_amount": 0.04,
    }


def _line_index(gcode: str, text: str) -> int:
    for index, line in enumerate(gcode.splitlines()):
        if text in line:
            return index
    raise AssertionError(f"{text!r} not found in gcode")
