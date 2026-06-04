from pathlib import Path
from collections import Counter

import ezdxf
import pytest

from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml
from dxfwiz.schemas import GeometryFile
from dxfwiz.yaml_io import load_yaml_file


INPUT_DIR = Path(__file__).resolve().parent / "dxf_clean"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def real_dxf_files() -> list[Path]:
    if not INPUT_DIR.exists():
        return []
    return sorted(
        path
        for path in INPUT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() == ".dxf"
    )


@pytest.mark.parametrize("source_path", real_dxf_files(), ids=lambda path: path.name)
def test_real_dxf_cleaning_outputs_fixed_dxf_and_geom_yaml(source_path):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = source_path.stem
    fixed_path = OUTPUT_DIR / f"{stem}_fixed.dxf"
    geom_path = OUTPUT_DIR / f"{stem}_geom.yaml"

    source_count = _entity_count(source_path)
    result = clean_dxf(
        source_path,
        fixed_path,
        CleanDxfConfig(
            gap_tolerance=0.005,
            duplicate_tolerance=0.0005,
            min_segment_length=0.001,
        ),
    )
    geom_data = write_geometry_yaml(
        fixed_path,
        geom_path,
        original_file=source_path.name,
        cleaned_file=fixed_path.name,
    )

    fixed_count = _entity_count(fixed_path)
    GeometryFile.model_validate(load_yaml_file(geom_path))

    assert fixed_path.exists()
    assert geom_path.exists()
    assert source_count > 0
    assert fixed_count > 0
    assert fixed_count == result.closed_loops + result.open_paths
    assert geom_data["summary"]["entity_count"] == fixed_count
    assert geom_data["units"]["length"] == "in"


def test_real_dxf_cleaning_summary_file_is_written():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUTPUT_DIR / "summary.txt"
    lines = [
        "file,source_entities,fixed_entities,closed_loops,open_paths,ignored,zero_length_removed,duplicates_removed,endpoints_snapped,warnings"
    ]

    for source_path in real_dxf_files():
        stem = source_path.stem
        fixed_path = OUTPUT_DIR / f"{stem}_fixed.dxf"
        source_count = _entity_count(source_path)
        fixed_count = _entity_count(fixed_path) if fixed_path.exists() else 0
        result = clean_dxf(source_path, fixed_path)
        fixed_count = _entity_count(fixed_path)
        lines.append(
            ",".join(
                [
                    source_path.name,
                    str(source_count),
                    str(fixed_count),
                    str(result.closed_loops),
                    str(result.open_paths),
                    str(result.entities_ignored),
                    str(result.zero_length_removed),
                    str(result.duplicates_removed),
                    str(result.endpoints_snapped),
                    "|".join(result.warnings),
                ]
            )
        )

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert summary_path.exists()
    assert len(lines) > 1


def test_2xintakev3_geometry_matches_screenshot_expectations():
    geom_path = OUTPUT_DIR / "2xintakev3_and_2xkickerv1_geom.yaml"
    data = load_yaml_file(geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert Counter(entity["shape"] for entity in data["entities"]) == {
        "circle": 50,
        "polyline_with_arcs": 8,
        "rectangle": 1,
    }

    roots = data["containment_tree"]
    assert len(roots) == 1
    frame = roots[0]
    assert frame["role"] == "frame"

    parts = frame["children"]
    assert len(parts) == 4
    assert sorted(len(part["children"]) for part in parts) == [5, 5, 22, 22]

    five_hole_parts = [part for part in parts if len(part["children"]) == 5]
    twenty_two_hole_parts = [part for part in parts if len(part["children"]) == 22]

    assert len(five_hole_parts) == 2
    assert len(twenty_two_hole_parts) == 2
    for part in five_hole_parts:
        assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
            "circle": 5
        }

    for part in twenty_two_hole_parts:
        assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
            "circle": 20,
            "polyline_with_arcs": 2,
        }


def test_intake_front_geometry_matches_screenshot_expectations():
    geom_path = OUTPUT_DIR / "intake_frontv2_geom.yaml"
    data = load_yaml_file(geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert Counter(entity["shape"] for entity in data["entities"]) == {
        "circle": 12,
        "polyline_with_arcs": 31,
    }

    roots = data["containment_tree"]
    assert len(roots) == 1
    part = roots[0]
    assert part["role"] == "part"
    assert len(part["children"]) == 42
    assert Counter(entities[child["entity"]]["shape"] for child in part["children"]) == {
        "circle": 12,
        "polyline_with_arcs": 30,
    }


def test_intakev4_geometry_matches_screenshot_expectations():
    geom_path = OUTPUT_DIR / "intakev4_geom.yaml"
    data = load_yaml_file(geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}

    assert data["units"]["length"] == "in"
    assert data["units"]["source"] == "guessed"

    frames = [node for node in data["containment_tree"] if node["role"] == "frame"]
    rectangles = [entity for entity in data["entities"] if entity["shape"] == "rectangle"]

    assert len(rectangles) == 5
    assert len(frames) == 1

    parts = frames[0]["children"]
    assert len(parts) == 9

    child_circle_counts = {
        part["entity"]: sum(
            1 for child in part["children"] if entities[child["entity"]]["shape"] == "circle"
        )
        for part in parts
    }
    assert sorted(child_circle_counts.values()) == [2, 2, 3, 3, 3, 5, 5, 15, 17]

    largest_part = max(parts, key=lambda part: _box_area(entities[part["entity"]]["bounding_box"]))
    assert child_circle_counts[largest_part["entity"]] == 17

    circular_parts = [part for part in parts if entities[part["entity"]]["shape"] == "circle"]
    assert len(circular_parts) == 1
    circular_part_children = [
        entities[child["entity"]] for child in circular_parts[0]["children"]
    ]
    assert len(circular_part_children) == 15
    assert sum(1 for child in circular_part_children if child["diameter"] < 0.5) == 14

    high_aspect_parts = [
        part
        for part in parts
        if _box_aspect_ratio(entities[part["entity"]]["bounding_box"]) > 3.0
    ]
    assert sorted(child_circle_counts[part["entity"]] for part in high_aspect_parts) == [2, 2, 3]
    assert sorted(
        count for count in child_circle_counts.values() if count in {3, 5}
    ) == [3, 3, 3, 5, 5]


def test_intakev4_largest_part_has_17_circular_holes():
    geom_path = OUTPUT_DIR / "intakev4_geom.yaml"
    data = load_yaml_file(geom_path)
    entities = {entity["id"]: entity for entity in data["entities"]}
    populated_frame = next(
        frame
        for frame in data["containment_tree"]
        if frame["role"] == "frame"
    )
    largest_part = max(
        populated_frame["children"],
        key=lambda part: _box_area(entities[part["entity"]]["bounding_box"]),
    )
    assert (
        sum(
            1
            for child in largest_part["children"]
            if entities[child["entity"]]["shape"] == "circle"
        )
        == 17
    )


def _entity_count(path: Path) -> int:
    return len(list(ezdxf.readfile(path).modelspace()))


def _box_area(box: dict) -> float:
    return (box["max"]["x"] - box["min"]["x"]) * (box["max"]["y"] - box["min"]["y"])


def _box_aspect_ratio(box: dict) -> float:
    width = box["max"]["x"] - box["min"]["x"]
    height = box["max"]["y"] - box["min"]["y"]
    return max(width, height) / min(width, height)
