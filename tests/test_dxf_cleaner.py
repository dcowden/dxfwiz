from pathlib import Path

import ezdxf

from dxfwiz.dxf import CleanDxfConfig, clean_dxf
from dxfwiz.dxf.geometry import write_geometry_yaml


def test_clean_closed_rectangle_from_unordered_lines(tmp_path):
    source = tmp_path / "rectangle.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((1, 1), (0, 1))
    msp.add_line((0, 0), (1, 0))
    msp.add_line((1, 0), (1, 1))
    msp.add_line((0, 1), (0, 0))
    doc.saveas(source)

    result = clean_dxf(source, fixed)

    assert result.closed_loops == 1
    assert result.open_paths == 0
    assert _lwpolylines(fixed)[0].closed


def test_gapped_rectangle_closes_when_within_tolerance(tmp_path):
    source = tmp_path / "gapped.dxf"
    fixed = tmp_path / "fixed.dxf"
    _write_gapped_rectangle(source, gap=0.001)

    result = clean_dxf(source, fixed, CleanDxfConfig(gap_tolerance=0.005))

    assert result.closed_loops == 1
    assert result.open_paths == 0
    assert result.endpoints_snapped > 0


def test_gapped_rectangle_stays_open_when_outside_tolerance(tmp_path):
    source = tmp_path / "gapped.dxf"
    fixed = tmp_path / "fixed.dxf"
    _write_gapped_rectangle(source, gap=0.001)

    result = clean_dxf(source, fixed, CleanDxfConfig(gap_tolerance=0.0001))

    assert result.closed_loops == 0
    assert result.open_paths > 0
    assert result.warnings


def test_duplicate_reversed_segment_removed(tmp_path):
    source = tmp_path / "duplicate.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 0))
    msp.add_line((1, 0), (0, 0))
    msp.add_line((1, 0), (1, 1))
    msp.add_line((1, 1), (0, 1))
    msp.add_line((0, 1), (0, 0))
    doc.saveas(source)

    result = clean_dxf(source, fixed)

    assert result.duplicates_removed == 1
    assert result.closed_loops == 1


def test_zero_length_segment_removed(tmp_path):
    source = tmp_path / "zero_length.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 0))
    msp.add_line((0, 0), (1, 0))
    doc.saveas(source)

    result = clean_dxf(source, fixed)

    assert result.zero_length_removed == 1
    assert result.open_paths == 1


def test_open_path_is_preserved_for_trace_operations(tmp_path):
    source = tmp_path / "open_path.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 0))
    msp.add_line((1, 0), (1, 1))
    msp.add_line((1, 1), (2, 1))
    doc.saveas(source)

    result = clean_dxf(source, fixed)

    assert result.closed_loops == 0
    assert result.open_paths == 1
    assert not _lwpolylines(fixed)[0].closed


def test_rounded_rectangle_preserves_arc_bulges(tmp_path):
    source = tmp_path / "rounded.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((1, 0), (3, 0))
    msp.add_arc(center=(3, 1), radius=1, start_angle=270, end_angle=360)
    msp.add_line((4, 1), (4, 2))
    msp.add_arc(center=(3, 2), radius=1, start_angle=0, end_angle=90)
    msp.add_line((3, 3), (1, 3))
    msp.add_arc(center=(1, 2), radius=1, start_angle=90, end_angle=180)
    msp.add_line((0, 2), (0, 1))
    msp.add_arc(center=(1, 1), radius=1, start_angle=180, end_angle=270)
    doc.saveas(source)

    result = clean_dxf(source, fixed)
    polyline = _lwpolylines(fixed)[0]

    assert result.closed_loops == 1
    assert polyline.closed
    assert any(abs(point[4]) > 0 for point in polyline.get_points())


def test_circle_is_preserved_as_closed_loop(tmp_path):
    source = tmp_path / "circle.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    doc.modelspace().add_circle(center=(1, 1), radius=0.25)
    doc.saveas(source)

    result = clean_dxf(source, fixed)

    assert result.closed_loops == 1
    assert fixed.exists()
    assert len(ezdxf.readfile(fixed).modelspace().query("CIRCLE")) == 1


def test_reorient_to_origin_is_disabled_by_default(tmp_path):
    source = tmp_path / "offset_rectangle.dxf"
    fixed = tmp_path / "fixed.dxf"
    _write_offset_rectangle(source)

    result = clean_dxf(source, fixed)

    assert result.origin_shift_x == 0.0
    assert result.origin_shift_y == 0.0
    assert _fixed_bounds(fixed) == (10.0, 20.0, 12.0, 21.0)


def test_reorient_to_origin_moves_fixed_dxf_and_geometry_yaml(tmp_path):
    source = tmp_path / "offset_rectangle.dxf"
    fixed = tmp_path / "fixed.dxf"
    geom = tmp_path / "geom.yaml"
    _write_offset_rectangle(source)

    result = clean_dxf(source, fixed, CleanDxfConfig(reorient_to_origin=True))
    geom_data = write_geometry_yaml(fixed, geom, original_file=source.name, cleaned_file=fixed.name)

    assert result.origin_shift_x == -10.0
    assert result.origin_shift_y == -20.0
    assert _fixed_bounds(fixed) == (0.0, 0.0, 2.0, 1.0)
    assert geom_data["summary"]["bounding_box"] == {
        "min": {"x": 0.0, "y": 0.0},
        "max": {"x": 2.0, "y": 1.0},
    }


def test_fixed_dxf_avoids_app_specific_xdata_for_cad_compatibility(tmp_path):
    source = tmp_path / "rectangle.dxf"
    fixed = tmp_path / "fixed.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 0))
    msp.add_line((1, 0), (1, 1))
    msp.add_line((1, 1), (0, 1))
    msp.add_line((0, 1), (0, 0))
    doc.saveas(source)

    clean_dxf(source, fixed)
    fixed_doc = ezdxf.readfile(fixed)

    assert not fixed_doc.appids.has_entry("DXFWIZ")
    assert all(not entity.has_xdata("DXFWIZ") for entity in fixed_doc.modelspace())


def _write_gapped_rectangle(path: Path, gap: float) -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1 - gap, 0))
    msp.add_line((1, 0), (1, 1 - gap))
    msp.add_line((1, 1), (gap, 1))
    msp.add_line((0, 1), (0, gap))
    doc.saveas(path)


def _write_offset_rectangle(path: Path) -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((10, 20), (12, 20))
    msp.add_line((12, 20), (12, 21))
    msp.add_line((12, 21), (10, 21))
    msp.add_line((10, 21), (10, 20))
    doc.saveas(path)


def _fixed_bounds(path: Path) -> tuple[float, float, float, float]:
    points = []
    for entity in ezdxf.readfile(path).modelspace():
        if entity.dxftype() == "LWPOLYLINE":
            points.extend((float(point[0]), float(point[1])) for point in entity.get_points("xy"))
        elif entity.dxftype() == "CIRCLE":
            center = entity.dxf.center
            radius = float(entity.dxf.radius)
            points.extend(
                [
                    (float(center.x) - radius, float(center.y) - radius),
                    (float(center.x) + radius, float(center.y) + radius),
                ]
            )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _lwpolylines(path: Path):
    return list(ezdxf.readfile(path).modelspace().query("LWPOLYLINE"))
