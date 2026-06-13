import ezdxf

from dxfwiz.schemas import GeometryFile, JobFile, MachineFile
from dxfwiz.toolpaths import ToolpathRequest, generate_toolpaths
from dxfwiz.toolpaths.core import compile_toolpath_plan
from dxfwiz.toolpaths.model import ToolpathPlan
from dxfwiz.toolpaths.posts import UccncPost
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
                    "type": "helical_contour",
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


def test_compile_toolpath_plan_preserves_bulged_dxf_arcs(tmp_path):
    dxf_path = tmp_path / "bulged.dxf"
    doc = ezdxf.new("R2000")
    doc.units = ezdxf.units.IN
    entity = doc.modelspace().add_lwpolyline(
        [(0, 0, 0), (1, 0, 0.41421356237), (1, 1, 0), (0, 1, 0)],
        format="xyb",
        close=True,
    )
    doc.saveas(dxf_path)

    machine = MachineFile.model_validate(load_yaml_file("examples/machine.yaml"))
    geometry = GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "source": "explicit_dxf", "confidence": 1.0, "coordinate_scale": 1.0},
            "source": {"original_file": "bulged.dxf", "cleaned_file": "bulged.dxf", "format": "dxf"},
            "summary": {
                "entity_count": 1,
                "closed_count": 1,
                "open_count": 0,
                "ignored_count": 0,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 1, "y": 1}},
            },
            "entity_map": [{"entity": "e1", "role": "part"}],
            "entities": [
                {
                    "id": "e1",
                    "type": "closed_loop",
                    "shape": "polyline_with_arcs",
                    "source_refs": [{"kind": "dxf_handle", "value": entity.dxf.handle}],
                    "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 1, "y": 1}},
                }
            ],
        }
    )
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "bulged",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "planner": "planner.yaml",
                "post": "post.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "operations": [
                {
                    "id": "op1",
                    "type": "contour",
                    "entity": "e1",
                    "tool": "t3",
                    "depth": 0.1,
                    "offset": "on",
                    "roughing": {
                        "enabled": False,
                        "depth_per_pass": 0.1,
                        "side_allowance": 0.0,
                        "bottom_allowance": 0.0,
                        "milling_direction": "climb",
                    },
                    "finishing": {"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
                }
            ],
        }
    )

    plan = compile_toolpath_plan(job, geometry, machine, fixed_dxf=dxf_path)

    assert [segment.type for segment in plan.source_paths[0].segments] == ["line", "arc", "line", "line"]


def test_uccnc_helical_pocket_prefer_arcs_outputs_g2_or_g3():
    machine = MachineFile.model_validate(load_yaml_file("examples/machine.yaml"))
    geometry = GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "source": "guessed", "confidence": 1.0, "coordinate_scale": 1.0},
            "source": {"original_file": "test.dxf", "cleaned_file": "test_fixed.dxf", "format": "dxf"},
            "summary": {
                "entity_count": 1,
                "closed_count": 1,
                "open_count": 0,
                "ignored_count": 0,
                "generated_count": 0,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 2, "y": 2}},
            },
            "entity_map": [{"entity": "e-hole", "role": "cutout"}],
            "entities": [
                {
                    "id": "e-hole",
                    "type": "closed_loop",
                    "shape": "circle",
                    "source_refs": [],
                    "center": {"x": 1.0, "y": 1.0},
                    "diameter": 1.25,
                    "bounding_box": {"min": {"x": 0.375, "y": 0.375}, "max": {"x": 1.625, "y": 1.625}},
                }
            ],
        }
    )
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "helical pocket arc test",
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
                    "type": "helical_pocket",
                    "entity": "e-hole",
                    "tool": "t5",
                    "depth": 0.26,
                    "hole_diameter": 1.25,
                    "pitch": 0.08,
                    "stepover_percent": 40,
                    "prefer_arcs": True,
                    "milling_direction": "climb",
                    "roughing": {
                        "enabled": True,
                        "depth_per_pass": 0.08,
                        "side_allowance": 0.01,
                        "bottom_allowance": 0.0,
                        "milling_direction": "climb",
                    },
                    "finishing": {"enabled": True, "side": True, "bottom": True, "passes": 1, "milling_direction": "climb"},
                }
            ],
        }
    )

    response = generate_toolpaths(ToolpathRequest(job=job, geometry=geometry, machine=machine))

    assert response.errors == []
    assert response.plan is not None
    assert any(move.type == "arc" for toolpath_pass in response.plan.passes for move in toolpath_pass.moves)
    assert "G3 " in response.gcode or "G2 " in response.gcode
    assert response.gcode.count("G1 ") < response.gcode.count("G3 ") + response.gcode.count("G2 ")


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


def test_toolpath_service_writes_separate_nc_file_per_tool_when_enabled():
    machine_data = load_yaml_file("examples/machine.yaml")
    machine_data["machine"]["separate_nc_file_per_tool"] = True
    machine = MachineFile.model_validate(machine_data)
    geometry = _fixture_geometry()
    job = JobFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "job": {
                "name": "simple split",
                "geometry_file": "geom.yaml",
                "machine": "machine.yaml",
                "post": "post.yaml",
                "planner": "planner.yaml",
            },
            "stock": {"material": "plywood", "thickness": 0.25, "z_zero": "stock_top", "origin_location": "bottom_left"},
            "coordinate_system": "G55",
            "generated_entities": [
                {"id": "wh1", "role": "screw_hole", "shape": "circle", "center": {"x": 0, "y": 0}, "diameter": 0.125},
                {"id": "wh2", "role": "screw_hole", "shape": "circle", "center": {"x": 1, "y": 0}, "diameter": 0.1875},
            ],
            "operations": [
                _drill_op("op-small", "wh1", "t1"),
                _drill_op("op-large", "wh2", "t3"),
            ],
        }
    )

    response = generate_toolpaths(ToolpathRequest(job=job, geometry=geometry, machine=machine))

    assert response.errors == []
    assert set(response.gcode_files) == {"simple_split_t1.nc", "simple_split_t3.nc"}
    assert "(op-small:" in response.gcode_files["simple_split_t1.nc"]
    assert "(op-large:" not in response.gcode_files["simple_split_t1.nc"]
    assert "(op-large:" in response.gcode_files["simple_split_t3.nc"]
    assert response.gcode_files["simple_split_t1.nc"].strip().endswith("M30")
    assert response.gcode_files["simple_split_t3.nc"].strip().endswith("M30")


def test_uccnc_post_omits_repeated_modal_axis_and_feed_values():
    plan = ToolpathPlan.model_validate(
        {
            "units": "in",
            "coordinate_system": "G55",
            "passes": [
                {
                    "id": "pass1",
                    "operation_id": "op1",
                    "kind": "rough_contour",
                    "tool": "t5",
                    "tool_diameter": 0.25,
                    "feed_rate": 70,
                    "z_bottom": -0.26,
                    "moves": [
                        {"type": "rapid", "x": 31.565, "y": 37.979, "z": 0.25},
                        {"type": "line", "z": -0.26, "feed": 70},
                        {"type": "line", "x": 31.569, "y": 37.981, "z": -0.26, "feed": 70},
                        {"type": "line", "x": 32.147, "y": 38.265, "z": -0.26, "feed": 70},
                    ],
                }
            ],
        }
    )

    gcode = UccncPost(precision=3).render(plan)

    assert "G1 Z-0.26 F70" in gcode
    assert "G1 X31.569 Y37.981 Z-0.26 F70" not in gcode
    assert "G1 X31.569 Y37.981" in gcode
    assert "G1 X32.147 Y38.265" in gcode
    assert gcode.count("F70") == 1
    assert gcode.count("Z-0.26") == 1


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
