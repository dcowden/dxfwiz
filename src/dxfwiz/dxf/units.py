from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


LengthUnit = Literal["in", "mm"]

INSUNITS_INCHES = 1
INSUNITS_MILLIMETERS = 4
MEASUREMENT_IMPERIAL = 0
MEASUREMENT_METRIC = 1

MAX_ROUTER_EXTENT_IN = 108.0
MIN_ROUTER_FEATURE_IN = 0.01

COMMON_INCH_HOLES = [
    1 / 16,
    3 / 32,
    1 / 8,
    5 / 32,
    0.159,
    3 / 16,
    0.196,
    0.201,
    1 / 4,
    0.257,
    0.266,
    0.3,
    5 / 16,
    3 / 8,
    1 / 2,
    5 / 8,
    3 / 4,
    1.125,
    2.0,
    3.125,
]
COMMON_MM_HOLES = [2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16, 20, 25]

COMMON_INCH_STOCK = [
    (12, 12),
    (12, 24),
    (18, 24),
    (24, 24),
    (24, 48),
    (48, 96),
]
COMMON_MM_STOCK = [
    (300, 300),
    (300, 600),
    (600, 600),
    (600, 1200),
    (1220, 2440),
]


@dataclass(frozen=True)
class UnitDecision:
    length: LengthUnit
    source: Literal["explicit_dxf", "guessed"]
    confidence: float
    coordinate_scale: float
    evidence: list[str]

    def to_yaml(self) -> dict[str, Any]:
        return {
            "length": self.length,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "coordinate_scale": round(self.coordinate_scale, 8),
            "evidence": self.evidence,
        }


def determine_length_units(
    doc_units: int,
    measurement: int | None,
    entities: list[dict[str, Any]],
    containment_tree: list[dict[str, Any]],
) -> UnitDecision:
    if doc_units == INSUNITS_INCHES:
        return UnitDecision(
            length="in",
            source="explicit_dxf",
            confidence=1.0,
            coordinate_scale=1.0,
            evidence=["DXF $INSUNITS declares inches"],
        )

    inch_score, inch_scale, inch_evidence = _best_inch_score(measurement, entities, containment_tree)
    mm_score, mm_evidence = _score_unit("mm", 1.0, measurement, entities, containment_tree)

    if mm_score > inch_score:
        length = "mm"
        coordinate_scale = 1.0
        winning_score = mm_score
        losing_score = inch_score
        evidence = mm_evidence
    else:
        length = "in"
        coordinate_scale = inch_scale
        winning_score = inch_score
        losing_score = mm_score
        evidence = inch_evidence

    difference = max(0.0, winning_score - losing_score)
    confidence = min(0.95, 0.55 + difference * 0.08)
    if doc_units == INSUNITS_MILLIMETERS:
        evidence.insert(0, "DXF $INSUNITS declares millimeters, but heuristic scoring is used")
    else:
        evidence.insert(0, "DXF $INSUNITS is missing or unitless")
    evidence.append(f"inch_score={inch_score:.2f}, mm_score={mm_score:.2f}")
    return UnitDecision(
        length=length,
        source="guessed",
        confidence=confidence,
        coordinate_scale=coordinate_scale,
        evidence=evidence,
    )


def _best_inch_score(
    measurement: int | None,
    entities: list[dict[str, Any]],
    containment_tree: list[dict[str, Any]],
) -> tuple[float, float, list[str]]:
    raw_score, raw_evidence = _score_unit("in", 1.0, measurement, entities, containment_tree)
    scaled_score, scaled_evidence = _score_unit(
        "in", 1 / 25.4, measurement, entities, containment_tree
    )
    if scaled_score > raw_score:
        scaled_evidence.insert(0, "raw DXF coordinates look like millimeters for an inch design")
        return scaled_score, 1 / 25.4, scaled_evidence
    return raw_score, 1.0, raw_evidence


def _score_unit(
    unit: LengthUnit,
    coordinate_scale: float,
    measurement: int | None,
    entities: list[dict[str, Any]],
    containment_tree: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    score = 0.0
    evidence: list[str] = []

    bbox = _summary_box(entities)
    if bbox is not None:
        width = bbox[0] * coordinate_scale
        height = bbox[1] * coordinate_scale
        width_in = _to_inches(width, unit)
        height_in = _to_inches(height, unit)
        max_extent_in = max(width_in, height_in)
        if max_extent_in > MAX_ROUTER_EXTENT_IN:
            score -= 5.0
            evidence.append(
                f"{unit} interpretation gives {max_extent_in:.1f} in max extent, above router limit"
            )
        elif 1.0 <= max_extent_in <= MAX_ROUTER_EXTENT_IN:
            score += 2.0
            evidence.append(
                f"{unit} interpretation gives plausible {max_extent_in:.1f} in max extent"
            )

    min_feature = _min_feature_in(entities, unit, coordinate_scale)
    if min_feature is not None:
        if min_feature < MIN_ROUTER_FEATURE_IN:
            score -= 2.0
            evidence.append(
                f"{unit} interpretation gives {min_feature:.4f} in minimum circular feature"
            )
        else:
            score += 1.0

    hole_matches = _common_hole_matches(unit, entities, coordinate_scale)
    if hole_matches:
        score += min(4.0, hole_matches / 5)
        evidence.append(f"{hole_matches} circular holes match common {unit} sizes")
        if unit == "in" and coordinate_scale != 1.0 and hole_matches >= 3:
            score += 2.0
            evidence.append(
                f"{hole_matches} raw circular holes match common inch sizes after 25.4 scaling"
            )

    stock_matches = _common_stock_matches(unit, entities, containment_tree, coordinate_scale)
    if stock_matches:
        score += stock_matches * 2.0
        evidence.append(f"{stock_matches} rectangle frame(s) match common {unit} stock sizes")

    if measurement == MEASUREMENT_IMPERIAL and unit == "in":
        score += 0.5
        evidence.append("DXF $MEASUREMENT gives weak imperial hint")
    elif measurement == MEASUREMENT_METRIC and unit == "mm":
        score += 0.5
        evidence.append("DXF $MEASUREMENT gives weak metric hint")

    return score, evidence


def _summary_box(entities: list[dict[str, Any]]) -> tuple[float, float] | None:
    boxes = [entity.get("bounding_box") for entity in entities if entity.get("bounding_box")]
    if not boxes:
        return None
    min_x = min(box["min"]["x"] for box in boxes)
    min_y = min(box["min"]["y"] for box in boxes)
    max_x = max(box["max"]["x"] for box in boxes)
    max_y = max(box["max"]["y"] for box in boxes)
    return max_x - min_x, max_y - min_y


def _min_feature_in(
    entities: list[dict[str, Any]], unit: LengthUnit, coordinate_scale: float
) -> float | None:
    diameters = [entity["diameter"] for entity in entities if entity.get("shape") == "circle"]
    if not diameters:
        return None
    return min(_to_inches(diameter * coordinate_scale, unit) for diameter in diameters)


def _common_hole_matches(
    unit: LengthUnit, entities: list[dict[str, Any]], coordinate_scale: float
) -> int:
    diameters = [
        entity["diameter"] * coordinate_scale
        for entity in entities
        if entity.get("shape") == "circle"
    ]
    if unit == "in":
        return sum(_matches_common(diameter, COMMON_INCH_HOLES, 0.005) for diameter in diameters)
    return sum(_matches_common(diameter, COMMON_MM_HOLES, 0.15) for diameter in diameters)


def _common_stock_matches(
    unit: LengthUnit,
    entities: list[dict[str, Any]],
    containment_tree: list[dict[str, Any]],
    coordinate_scale: float,
) -> int:
    entity_by_id = {entity["id"]: entity for entity in entities}
    frame_ids = [node["entity"] for node in containment_tree if node["role"] == "frame"]
    matches = 0
    for frame_id in frame_ids:
        frame = entity_by_id[frame_id]
        box = frame.get("bounding_box")
        if not box:
            continue
        dims = sorted(
            [
                (box["max"]["x"] - box["min"]["x"]) * coordinate_scale,
                (box["max"]["y"] - box["min"]["y"]) * coordinate_scale,
            ]
        )
        common_sizes = COMMON_INCH_STOCK if unit == "in" else COMMON_MM_STOCK
        tolerance = 0.1 if unit == "in" else 3.0
        if any(_dims_close(dims, sorted(size), tolerance) for size in common_sizes):
            matches += 1
    return matches


def _matches_common(value: float, common_values: list[float], absolute_tolerance: float) -> bool:
    for common_value in common_values:
        tolerance = max(absolute_tolerance, abs(common_value) * 0.02)
        if abs(value - common_value) <= tolerance:
            return True
    return False


def _dims_close(values: list[float], common_values: list[float], absolute_tolerance: float) -> bool:
    return all(
        abs(value - common) <= max(absolute_tolerance, common * 0.02)
        for value, common in zip(values, common_values)
    )


def _to_inches(value: float, unit: LengthUnit) -> float:
    return value if unit == "in" else value / 25.4
