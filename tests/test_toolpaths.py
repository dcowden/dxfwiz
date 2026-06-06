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
                {"id": "op1", "type": "drill", "entity": "wh1", "tool": "t5", "depth": 0.2, "peck_depth": 0.1},
                {"id": "op2", "type": "helical_drill", "entity": "e1", "tool": "t5", "depth": 0.26},
                {"id": "op3", "type": "contour", "entity": "e2", "tool": "t5", "depth": 0.26, "offset": "outside"},
            ],
        }
    )

    response = generate_toolpaths(ToolpathRequest(job=job, geometry=geometry, machine=machine))

    assert response.errors == []
    assert "G20" in response.gcode
    assert "G90" in response.gcode
    assert "G55" in response.gcode
    assert "G1 Z-0.1" in response.gcode
    assert "G3" in response.gcode
    assert "(op3:" in response.gcode
    assert "M30" in response.gcode
