from __future__ import annotations

from pathlib import Path

import ezdxf

from dxfwiz.dxf import CleanDxfConfig
from dxfwiz.nesting.extract_parts import extract_parts


FIXTURE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "nesting"


def test_extract_2xintake_parts():
    result = extract_parts(
        FIXTURE_DIR / "2xintake" / "2xintakev3_and_2xkickerv1.dxf",
        OUTPUT_DIR / "2xintake" / "parts",
        clean_config=CleanDxfConfig(gap_tolerance=0.005, duplicate_tolerance=0.0005),
        name_prefix="2xintake_part",
    )

    assert len(result.parts) == 4
    assert sorted(_entity_count(part.path) for part in result.parts) == [6, 6, 23, 23]


def test_extract_intakev4_parts():
    result = extract_parts(
        FIXTURE_DIR / "intakev4" / "intakev4.dxf",
        OUTPUT_DIR / "intakev4" / "parts",
        clean_config=CleanDxfConfig(gap_tolerance=0.005, duplicate_tolerance=0.0005),
        name_prefix="intakev4_part",
    )

    assert len(result.parts) == 9
    assert sorted(_entity_count(part.path) for part in result.parts) == [
        3,
        3,
        4,
        4,
        4,
        6,
        6,
        16,
        18,
    ]


def _entity_count(path: Path) -> int:
    return len(list(ezdxf.readfile(path).modelspace()))
