from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, isclose, radians, tan
from pathlib import Path
from typing import Iterable

import ezdxf


Point = tuple[float, float]


@dataclass(frozen=True)
class CleanDxfConfig:
    gap_tolerance: float = 0.005
    duplicate_tolerance: float = 0.0005
    min_segment_length: float = 0.001
    preserve_arcs: bool = True


@dataclass
class CleanDxfResult:
    input_path: Path
    output_path: Path
    entities_read: int = 0
    entities_ignored: int = 0
    zero_length_removed: int = 0
    duplicates_removed: int = 0
    endpoints_snapped: int = 0
    closed_loops: int = 0
    open_paths: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class Segment:
    kind: str
    start: Point
    end: Point
    bulge: float = 0.0
    source_handle: str | None = None

    def reversed(self) -> "Segment":
        return Segment(
            kind=self.kind,
            start=self.end,
            end=self.start,
            bulge=-self.bulge,
            source_handle=self.source_handle,
        )

    @property
    def length_key(self) -> tuple[Point, Point]:
        return _canonical_segment_key(self.start, self.end)


def clean_dxf(
    input_path: str | Path,
    output_path: str | Path,
    config: CleanDxfConfig | None = None,
) -> CleanDxfResult:
    config = config or CleanDxfConfig()
    input_path = Path(input_path)
    output_path = Path(output_path)
    result = CleanDxfResult(input_path=input_path, output_path=output_path)

    doc = ezdxf.readfile(input_path)
    msp = doc.modelspace()
    source_entities = list(msp)
    result.entities_read = len(source_entities)

    segments: list[Segment] = []
    closed_entities = []
    for entity in source_entities:
        dxftype = entity.dxftype()
        if dxftype == "LINE":
            segments.append(_line_segment(entity))
        elif dxftype == "ARC":
            segments.append(_arc_segment(entity))
        elif dxftype == "LWPOLYLINE":
            segments.extend(_lwpolyline_segments(entity))
        elif dxftype == "POLYLINE" and entity.is_2d_polyline:
            segments.extend(_polyline_segments(entity))
        elif dxftype == "CIRCLE":
            closed_entities.append(entity)
        else:
            result.entities_ignored += 1
            result.warnings.append(f"Ignored unsupported DXF entity {dxftype}")

    segments, zero_length_removed = _remove_zero_length(segments, config.min_segment_length)
    result.zero_length_removed = zero_length_removed

    segments, endpoints_snapped = _snap_endpoints(segments, config.gap_tolerance)
    result.endpoints_snapped = endpoints_snapped

    segments, duplicates_removed = _remove_duplicates(segments, config.duplicate_tolerance)
    result.duplicates_removed = duplicates_removed

    chains = _build_chains(segments, config.gap_tolerance)
    closed_chains = [chain for chain in chains if _points_close(chain[0].start, chain[-1].end, config.gap_tolerance)]
    open_chains = [chain for chain in chains if chain not in closed_chains]

    result.closed_loops = len(closed_chains) + len(closed_entities)
    result.open_paths = len(open_chains)
    if open_chains:
        result.warnings.append(f"{len(open_chains)} open path(s) remain after cleanup")

    out_doc = ezdxf.new("R2000")
    out_doc.units = doc.units
    out_msp = out_doc.modelspace()

    for index, chain in enumerate(closed_chains, start=1):
        _add_lwpolyline(out_msp, chain, closed=True, layer="DXFWIZ_CLOSED")

    for index, circle in enumerate(closed_entities, start=1):
        center = circle.dxf.center
        out_msp.add_circle(
            center=(center.x, center.y),
            radius=circle.dxf.radius,
            dxfattribs={"layer": "DXFWIZ_CLOSED"},
        )

    for index, chain in enumerate(open_chains, start=1):
        _add_lwpolyline(out_msp, chain, closed=False, layer="DXFWIZ_OPEN")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(output_path)
    return result


def _line_segment(entity) -> Segment:
    start = entity.dxf.start
    end = entity.dxf.end
    return Segment(
        kind="line",
        start=(float(start.x), float(start.y)),
        end=(float(end.x), float(end.y)),
        source_handle=entity.dxf.handle,
    )


def _arc_segment(entity) -> Segment:
    center = entity.dxf.center
    radius = float(entity.dxf.radius)
    start_angle = float(entity.dxf.start_angle)
    end_angle = float(entity.dxf.end_angle)
    start = _polar_point((float(center.x), float(center.y)), radius, start_angle)
    end = _polar_point((float(center.x), float(center.y)), radius, end_angle)
    delta = (end_angle - start_angle) % 360.0
    if isclose(delta, 0.0):
        delta = 360.0
    return Segment(
        kind="arc",
        start=start,
        end=end,
        bulge=tan(radians(delta) / 4.0),
        source_handle=entity.dxf.handle,
    )


def _lwpolyline_segments(entity) -> list[Segment]:
    points = list(entity.get_points("xyb"))
    if len(points) < 2:
        return []
    segments: list[Segment] = []
    pairs = list(zip(points, points[1:]))
    if entity.closed:
        pairs.append((points[-1], points[0]))
    for start, end in pairs:
        segments.append(
            Segment(
                kind="arc" if abs(float(start[2])) > 0 else "line",
                start=(float(start[0]), float(start[1])),
                end=(float(end[0]), float(end[1])),
                bulge=float(start[2]),
                source_handle=entity.dxf.handle,
            )
        )
    return segments


def _polyline_segments(entity) -> list[Segment]:
    vertices = list(entity.vertices)
    if len(vertices) < 2:
        return []
    points = [
        (
            float(vertex.dxf.location.x),
            float(vertex.dxf.location.y),
            float(vertex.dxf.bulge) if vertex.dxf.hasattr("bulge") else 0.0,
        )
        for vertex in vertices
    ]
    pairs = list(zip(points, points[1:]))
    if entity.is_closed:
        pairs.append((points[-1], points[0]))

    segments: list[Segment] = []
    for start, end in pairs:
        segments.append(
            Segment(
                kind="arc" if abs(float(start[2])) > 0 else "line",
                start=(float(start[0]), float(start[1])),
                end=(float(end[0]), float(end[1])),
                bulge=float(start[2]),
                source_handle=entity.dxf.handle,
            )
        )
    return segments


def _remove_zero_length(segments: Iterable[Segment], min_length: float) -> tuple[list[Segment], int]:
    kept = []
    removed = 0
    for segment in segments:
        if _distance(segment.start, segment.end) <= min_length:
            removed += 1
        else:
            kept.append(segment)
    return kept, removed


def _snap_endpoints(segments: list[Segment], tolerance: float) -> tuple[list[Segment], int]:
    if tolerance <= 0:
        return segments, 0

    points = [point for segment in segments for point in (segment.start, segment.end)]
    clusters: list[list[Point]] = []
    for point in points:
        for cluster in clusters:
            if any(_distance(point, existing) <= tolerance for existing in cluster):
                cluster.append(point)
                break
        else:
            clusters.append([point])

    replacements: dict[Point, Point] = {}
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        x = sum(point[0] for point in cluster) / len(cluster)
        y = sum(point[1] for point in cluster) / len(cluster)
        snapped = (x, y)
        for point in cluster:
            replacements[point] = snapped

    snapped_segments = [
        Segment(
            kind=segment.kind,
            start=replacements.get(segment.start, segment.start),
            end=replacements.get(segment.end, segment.end),
            bulge=segment.bulge,
            source_handle=segment.source_handle,
        )
        for segment in segments
    ]
    changed = sum(1 for original, new in zip(segments, snapped_segments) if original.start != new.start)
    changed += sum(1 for original, new in zip(segments, snapped_segments) if original.end != new.end)
    return snapped_segments, changed


def _remove_duplicates(
    segments: Iterable[Segment], tolerance: float
) -> tuple[list[Segment], int]:
    kept: list[Segment] = []
    removed = 0
    keys: set[tuple[tuple[int, int], tuple[int, int], int]] = set()
    for segment in segments:
        start, end = _canonical_segment_key(segment.start, segment.end)
        key = (
            _quantize_point(start, tolerance),
            _quantize_point(end, tolerance),
            round(segment.bulge, 9),
        )
        reverse_key = (
            _quantize_point(start, tolerance),
            _quantize_point(end, tolerance),
            round(-segment.bulge, 9),
        )
        if key in keys or reverse_key in keys:
            removed += 1
            continue
        keys.add(key)
        kept.append(segment)
    return kept, removed


def _build_chains(segments: list[Segment], tolerance: float) -> list[list[Segment]]:
    remaining = segments[:]
    chains: list[list[Segment]] = []

    while remaining:
        chain = [remaining.pop(0)]
        extended = True
        while extended:
            extended = False
            for index, candidate in enumerate(remaining):
                if _points_close(chain[-1].end, candidate.start, tolerance):
                    chain.append(candidate)
                elif _points_close(chain[-1].end, candidate.end, tolerance):
                    chain.append(candidate.reversed())
                elif _points_close(chain[0].start, candidate.end, tolerance):
                    chain.insert(0, candidate)
                elif _points_close(chain[0].start, candidate.start, tolerance):
                    chain.insert(0, candidate.reversed())
                else:
                    continue
                remaining.pop(index)
                extended = True
                break
        chains.append(chain)
    return chains


def _add_lwpolyline(layout, chain: list[Segment], closed: bool, layer: str):
    points = []
    for segment in chain:
        points.append((segment.start[0], segment.start[1], segment.bulge))
    if not closed:
        points.append((chain[-1].end[0], chain[-1].end[1], 0.0))
    return layout.add_lwpolyline(
        points,
        format="xyb",
        close=closed,
        dxfattribs={"layer": layer},
    )


def _polar_point(center: Point, radius: float, angle_degrees: float) -> Point:
    angle = radians(angle_degrees)
    from math import cos, sin

    return (center[0] + radius * cos(angle), center[1] + radius * sin(angle))


def _distance(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _points_close(a: Point, b: Point, tolerance: float) -> bool:
    return _distance(a, b) <= tolerance


def _canonical_segment_key(start: Point, end: Point) -> tuple[Point, Point]:
    return (start, end) if start <= end else (end, start)


def _quantize_point(point: Point, tolerance: float) -> tuple[int, int]:
    scale = tolerance if tolerance > 0 else 1e-12
    return (round(point[0] / scale), round(point[1] / scale))


def _angle_between(start: Point, end: Point) -> float:
    return atan2(end[1] - start[1], end[0] - start[0])
