from __future__ import annotations

from dxfwiz.planning.tool_selector import select_largest_single_tool
from dxfwiz.schemas import GeometryFile, MachineFile


def test_selector_picks_largest_tool_that_fits_common_201_holes():
    geometry = _geometry(
        [
            _entity("part", "rectangle", (0, 0, 4, 4)),
            _circle("hole", 2, 2, 0.201),
        ],
        [{"entity": "part", "role": "part", "children": [{"entity": "hole", "role": "cutout"}]}],
    )

    result = select_largest_single_tool(geometry, _machine())

    assert result.tool.id == "t3"
    assert any(rejection.tool == "t5" and rejection.entity == "hole" for rejection in result.rejected)


def test_selector_uses_one_eighth_when_smaller_feature_requires_it():
    geometry = _geometry(
        [
            _entity("part", "rectangle", (0, 0, 4, 4)),
            _circle("small-hole", 2, 2, 0.13),
        ],
        [{"entity": "part", "role": "part", "children": [{"entity": "small-hole", "role": "cutout"}]}],
    )

    result = select_largest_single_tool(geometry, _machine())

    assert result.tool.id == "t1"
    assert {rejection.tool for rejection in result.rejected} >= {"t3", "t5"}


def test_selector_rejects_tools_that_do_not_fit_narrow_non_circular_cutout():
    geometry = _geometry(
        [
            _entity("part", "rectangle", (0, 0, 4, 4)),
            _entity("slot", "rectangle", (1, 1, 3, 1.16)),
        ],
        [{"entity": "part", "role": "part", "children": [{"entity": "slot", "role": "cutout"}]}],
    )

    result = select_largest_single_tool(geometry, _machine())

    assert result.tool.id == "t1"
    assert any(rejection.entity == "slot" and rejection.tool == "t3" for rejection in result.rejected)


def test_selector_ignores_external_contours_when_no_internal_features_constrain_tool():
    geometry = _geometry(
        [_entity("part", "rectangle", (0, 0, 4, 4))],
        [{"entity": "part", "role": "part"}],
    )

    result = select_largest_single_tool(geometry, _machine())

    assert result.tool.id == "t5"
    assert result.rejected == []


def _machine() -> MachineFile:
    return MachineFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "machine": {
                "name": "selector test router",
                "type": "router",
                "axes": 3,
                "max_tools": 1,
                "clear_z": 0.25,
                "workholding": ["screws"],
                "part_holding": ["tabs"],
                "work_envelope": {"x": {"min": 0, "max": 96}, "y": {"min": 0, "max": 50}, "z": {"min": -3, "max": 0}},
                "coordinate_system": {"x_positive": "right", "y_positive": "up", "origin": "bottom_left"},
                "spindle": {"type": "er11", "max_rpm": 24000},
            },
            "tools": [
                _tool("t1", 0.125),
                _tool("t3", 0.1875),
                _tool("t5", 0.25),
            ],
        }
    )


def _tool(tool_id: str, diameter: float) -> dict:
    return {
        "id": tool_id,
        "description": f"{diameter} in tool",
        "end_type": "flat",
        "flute_spiral": "upcut",
        "diameter": diameter,
        "flutes": 2,
        "speed": 18000,
        "feed_rate": 80,
        "plunge_rate": 25,
        "depth_per_pass": diameter / 2,
    }


def _geometry(entities: list[dict], entity_map: list[dict]) -> GeometryFile:
    return GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "source": "guessed", "confidence": 1.0, "coordinate_scale": 1.0},
            "source": {"original_file": "selector.dxf", "cleaned_file": "selector_fixed.dxf", "format": "dxf"},
            "summary": {
                "entity_count": len(entities),
                "closed_count": len(entities),
                "open_count": 0,
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 4, "y": 4}},
            },
            "entity_map": entity_map,
            "entities": entities,
        }
    )


def _entity(entity_id: str, shape: str, bounds: tuple[float, float, float, float]) -> dict:
    min_x, min_y, max_x, max_y = bounds
    return {
        "id": entity_id,
        "type": "closed_loop",
        "shape": shape,
        "source_refs": [],
        "bounding_box": {"min": {"x": min_x, "y": min_y}, "max": {"x": max_x, "y": max_y}},
    }


def _circle(entity_id: str, x: float, y: float, diameter: float) -> dict:
    radius = diameter / 2
    entity = _entity(entity_id, "circle", (x - radius, y - radius, x + radius, y + radius))
    entity["center"] = {"x": x, "y": y}
    entity["diameter"] = diameter
    return entity
