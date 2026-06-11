from __future__ import annotations

import math
from pathlib import Path

import ezdxf

from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml
from dxfwiz.planning.tool_selector import select_largest_single_tool
from dxfwiz.schemas import GeometryFile, MachineFile
from dxfwiz.yaml_io import load_yaml_file


def test_arc_detection_recover_preserves_source_arcs_and_recovers_many_line_arcs(tmp_path: Path):
    source = tmp_path / "arc_detection_source.dxf"
    fixed = tmp_path / "arc_detection_fixed.dxf"
    geom_path = tmp_path / "arc_detection_geom.yaml"
    _write_arc_detection_fixture(source)

    result = clean_dxf(
        source,
        fixed,
        CleanDxfConfig(
            gap_tolerance=0.002,
            duplicate_tolerance=0.0005,
            min_segment_length=0.0001,
            arc_detection="RECOVER",
            arc_tolerance=0.003,
        ),
    )
    geom_data = write_geometry_yaml(fixed, geom_path, original_file=source.name, cleaned_file=fixed.name)
    fixed_doc = ezdxf.readfile(fixed)
    lwpolylines = [entity for entity in fixed_doc.modelspace() if entity.dxftype() == "LWPOLYLINE"]
    bulged_polylines = [
        entity
        for entity in lwpolylines
        if any(abs(point[4]) > 1e-9 for point in entity.get_points())
    ]

    assert result.arcs_recovered >= 1
    assert len(bulged_polylines) >= 3
    assert any(entity["shape"] == "polyline_with_arcs" for entity in geom_data["entities"])


def test_arc_detection_fixture_selects_three_sixteenths_tool_after_recovery(tmp_path: Path):
    source = tmp_path / "arc_tool_source.dxf"
    fixed = tmp_path / "arc_tool_fixed.dxf"
    geom_path = tmp_path / "arc_tool_geom.yaml"
    _write_arc_detection_fixture(source)
    clean_dxf(
        source,
        fixed,
        CleanDxfConfig(
            gap_tolerance=0.002,
            duplicate_tolerance=0.0005,
            min_segment_length=0.0001,
            arc_detection="RECOVER",
            arc_tolerance=0.003,
        ),
    )
    write_geometry_yaml(fixed, geom_path, original_file=source.name, cleaned_file=fixed.name)
    geometry = GeometryFile.model_validate(load_yaml_file(geom_path))
    machine = _machine_with_three_tool_sizes()

    result = select_largest_single_tool(geometry, machine, fixed.read_text(encoding="utf-8", errors="ignore"))

    assert result.tool.id == "t3"
    assert result.tool.diameter == 0.1875


def _write_arc_detection_fixture(path: Path) -> None:
    doc = ezdxf.new("R2000")
    doc.units = ezdxf.units.IN
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (2, 0), (2, 2), (0, 2)], close=True)
    _add_many_line_circle(msp, center=(0.75, 1.0), radius=0.1005, segments=48)
    msp.add_arc(center=(1.35, 1.0), radius=0.1005, start_angle=0, end_angle=180)
    msp.add_arc(center=(1.35, 1.0), radius=0.1005, start_angle=180, end_angle=360)
    msp.add_lwpolyline(
        [
            (1.1, 0.35, 0.4142135624),
            (1.35, 0.6, 0.4142135624),
            (1.6, 0.35, 0.4142135624),
            (1.35, 0.1, 0.4142135624),
        ],
        format="xyb",
        close=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)


def _add_many_line_circle(msp, center: tuple[float, float], radius: float, segments: int) -> None:
    points = [
        (
            center[0] + math.cos(2 * math.pi * index / segments) * radius,
            center[1] + math.sin(2 * math.pi * index / segments) * radius,
        )
        for index in range(segments)
    ]
    for start, end in zip(points, [*points[1:], points[0]], strict=True):
        msp.add_line(start, end)


def _machine_with_three_tool_sizes() -> MachineFile:
    return MachineFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "machine": {
                "name": "test",
                "type": "router",
                "axes": 3,
                "max_tools": 1,
                "clear_z": 0.25,
                "operation_sort": ["tool", "group", "nest_order"],
                "workholding": ["screws"],
                "part_holding": ["tabs"],
                "work_envelope": {"x": {"min": 0, "max": 10}, "y": {"min": 0, "max": 10}, "z": {"min": -1, "max": 1}},
                "coordinate_system": {"x_positive": "right", "y_positive": "up", "origin": "bottom_left"},
                "spindle": {"type": "er11", "max_rpm": 24000, "min_rpm": 5000},
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
        "description": tool_id,
        "end_type": "flat",
        "flute_spiral": "upcut",
        "diameter": diameter,
        "flutes": 2,
        "speed": 18000,
        "feed_rate": 60,
        "plunge_rate": 20,
        "depth_per_pass": diameter / 2,
        "step_over": diameter / 2,
    }
