from __future__ import annotations

from pathlib import Path

from dxfwiz.dxf import clean_dxf, write_geometry_yaml
from dxfwiz.nesting.dxf_writer import write_nested_dxf
from dxfwiz.nesting.engine import (
    load_part_shapes,
    minimum_stock_width,
    nest_parts,
    stock_width_from_source,
)
from dxfwiz.nesting.inputs import load_part_shapes_from_dxf_inputs
from dxfwiz.nesting.report import write_nesting_index, write_nesting_png
from dxfwiz.yaml_io import load_yaml_file
from shapely.geometry import box


FIXTURE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "output" / "nesting"


def test_nesting_engine_generates_visual_artifacts_for_fixture_cases():
    cases = []
    for name, source_names, expected_part_count, expand_inputs in [
        ("2xintake", ("2xintakev3_and_2xkickerv1.dxf",), 4, False),
        ("intakev4", ("intakev4.dxf",), 9, False),
        ("mixed", ("2xintakev3_and_2xkickerv1.dxf", "intakev4.dxf"), 13, True),
    ]:
        case_dir = FIXTURE_DIR / name
        source_dxfs = [case_dir / source_name for source_name in source_names]
        output_dir = OUTPUT_ROOT / name
        if expand_inputs:
            parts = load_part_shapes_from_dxf_inputs(source_dxfs, output_dir / "parts")
        else:
            parts = load_part_shapes(sorted((case_dir / "parts").glob("*.dxf")))
        spacing = 0.375
        stock_width = max(
            *(stock_width_from_source(source_dxf) for source_dxf in source_dxfs),
            minimum_stock_width(parts, spacing=spacing) + spacing,
        )
        result = nest_parts(
            parts,
            stock_width=stock_width,
            spacing=spacing,
        )

        assert not result.unplaced
        assert len(parts) == expected_part_count
        assert len(result.placements) == len(parts)
        assert result.used_width <= result.stock_width
        _assert_clearance(result)

        image_path = output_dir / "nesting.png"
        write_nesting_png(
            source_dxf=source_dxfs,
            result=result,
            output_path=image_path,
            title=name,
        )
        assert image_path.exists()

        nested_dxf_path = output_dir / "nested.dxf"
        nested_fixed_path = output_dir / "nested_fixed.dxf"
        nested_geom_path = output_dir / "nested_geom.yaml"
        write_nested_dxf(result, parts, nested_dxf_path, border=1.0)
        clean_dxf(nested_dxf_path, nested_fixed_path)
        write_geometry_yaml(
            nested_fixed_path,
            nested_geom_path,
            original_file=nested_dxf_path.name,
            cleaned_file=nested_fixed_path.name,
            length_units="in",
        )
        _assert_nested_geometry_counts(nested_geom_path, len(parts))

        cases.append(
            {
                "name": name,
                "image": Path(name) / "nesting.png",
                "placed": len(result.placements),
                "unplaced": len(result.unplaced),
                "used_width": result.used_width,
                "used_height": result.used_height,
            }
        )

    index_path = write_nesting_index(OUTPUT_ROOT, cases)
    assert index_path.exists()
    index_html = index_path.read_text(encoding="utf-8")
    assert "2xintake" in index_html
    assert "intakev4" in index_html
    assert "mixed" in index_html
    assert "mixed/nesting.png" in index_html


def _assert_clearance(result):
    stock = box(0, 0, result.stock_width, max(result.stock_height, result.used_height))
    footprints = [placement.footprint for placement in result.placements]
    assert all(stock.covers(footprint) for footprint in footprints)
    for index, footprint in enumerate(footprints):
        for other in footprints[index + 1 :]:
            assert not footprint.intersects(other)


def _assert_nested_geometry_counts(geom_path: Path, expected_part_count: int) -> None:
    data = load_yaml_file(geom_path)
    frames = [node for node in data["entity_map"] if node["role"] == "frame"]
    assert len(frames) == 1
    assert len(frames[0]["children"]) == expected_part_count
