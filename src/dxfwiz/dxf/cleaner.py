from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import atan2, cos, isclose, pi, radians, sin, tan
from pathlib import Path
from typing import Iterable, Literal

import ezdxf


Point = tuple[float, float]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanDxfConfig:
    gap_tolerance: float = 0.005
    duplicate_tolerance: float = 0.0005
    min_segment_length: float = 0.001
    preserve_arcs: bool = True
    arc_detection: Literal["OFF", "FOR_PLANNING", "RECOVER"] = "OFF"
    arc_tolerance: float = 0.002
    reorient_to_origin: bool = False


@dataclass
class CleanDxfResult:
    input_path: Path
    output_path: Path
    entities_read: int = 0
    entities_ignored: int = 0
    zero_length_removed: int = 0
    duplicates_removed: int = 0
    endpoints_snapped: int = 0
    arcs_recovered: int = 0
    origin_shift_x: float = 0.0
    origin_shift_y: float = 0.0
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
    logger.info("Cleaning DXF %s -> %s", input_path, output_path)
    logger.debug("DXF cleaner config: %s", config)

    doc = ezdxf.readfile(input_path)
    msp = doc.modelspace()
    source_entities = list(msp)
    result.entities_read = len(source_entities)
    logger.info("Read %d source DXF entities", result.entities_read)

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
    if config.arc_detection == "RECOVER" and config.arc_tolerance > 0:
        chains, result.arcs_recovered = _recover_arcs_in_chains(chains, config.arc_tolerance)
    closed_chains = [chain for chain in chains if _points_close(chain[0].start, chain[-1].end, config.gap_tolerance)]
    open_chains = [chain for chain in chains if chain not in closed_chains]

    result.closed_loops = len(closed_chains) + len(closed_entities)
    result.open_paths = len(open_chains)
    if open_chains:
        result.warnings.append(f"{len(open_chains)} open path(s) remain after cleanup")
        logger.info("%d open path(s) remain after cleanup", len(open_chains))

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

    if config.reorient_to_origin:
        result.origin_shift_x, result.origin_shift_y = _translate_modelspace_to_origin(out_msp)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(output_path)
    logger.info(
        "Wrote fixed DXF %s (%d closed loop(s), %d open path(s), %d duplicate(s) removed, %d arc(s) recovered)",
        output_path,
        result.closed_loops,
        result.open_paths,
        result.duplicates_removed,
        result.arcs_recovered,
    )
    return result


def _translate_modelspace_to_origin(msp) -> tuple[float, float]:
    bounds = _modelspace_bounds(msp)
    if bounds is None:
        return 0.0, 0.0
    min_x, min_y, _max_x, _max_y = bounds
    dx = -min_x
    dy = -min_y
    if abs(dx) <= 1e-12 and abs(dy) <= 1e-12:
        return 0.0, 0.0
    for entity in msp:
        _translate_entity(entity, dx, dy)
    return dx, dy


def _modelspace_bounds(msp) -> tuple[float, float, float, float] | None:
    points: list[Point] = []
    for entity in msp:
        if entity.dxftype() == "CIRCLE":
            center = entity.dxf.center
            radius = float(entity.dxf.radius)
            points.extend(
                [
                    (float(center.x) - radius, float(center.y) - radius),
                    (float(center.x) + radius, float(center.y) + radius),
                ]
            )
        elif entity.dxftype() == "LWPOLYLINE":
            points.extend((float(point[0]), float(point[1])) for point in entity.get_points("xy"))
        elif entity.dxftype() == "LINE":
            points.extend(
                [
                    (float(entity.dxf.start.x), float(entity.dxf.start.y)),
                    (float(entity.dxf.end.x), float(entity.dxf.end.y)),
                ]
            )
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _translate_entity(entity, dx: float, dy: float) -> None:
    try:
        entity.translate(dx, dy, 0.0)
        return
    except Exception:
        pass
    if entity.dxftype() == "CIRCLE":
        center = entity.dxf.center
        entity.dxf.center = (float(center.x) + dx, float(center.y) + dy, float(center.z))
    elif entity.dxftype() == "LWPOLYLINE":
        entity.set_points(
            [
                (float(x) + dx, float(y) + dy, float(start_width), float(end_width), float(bulge))
                for x, y, start_width, end_width, bulge in entity.get_points("xyseb")
            ],
            format="xyseb",
        )
    elif entity.dxftype() == "LINE":
        start = entity.dxf.start
        end = entity.dxf.end
        entity.dxf.start = (float(start.x) + dx, float(start.y) + dy, float(start.z))
        entity.dxf.end = (float(end.x) + dx, float(end.y) + dy, float(end.z))


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


def _recover_arcs_in_chains(chains: list[list[Segment]], tolerance: float) -> tuple[list[list[Segment]], int]:
    recovered_count = 0
    result: list[list[Segment]] = []
    for chain in chains:
        recovered_chain, chain_count = _recover_arcs_in_chain(chain, tolerance)
        result.append(recovered_chain)
        recovered_count += chain_count
    return result, recovered_count


def _recover_arcs_in_chain(chain: list[Segment], tolerance: float) -> tuple[list[Segment], int]:
    recovered: list[Segment] = []
    index = 0
    count = 0
    while index < len(chain):
        if chain[index].kind != "line":
            recovered.append(chain[index])
            index += 1
            continue
        best: tuple[int, Segment] | None = None
        max_end = index
        while max_end < len(chain) and chain[max_end].kind == "line":
            max_end += 1
        for end in range(index + 3, max_end + 1):
            points = _chain_points(chain[index:end])
            arc = _fit_arc_segment(points, tolerance)
            if arc is not None:
                best = (end, arc)
        if best is None:
            recovered.append(chain[index])
            index += 1
            continue
        end, arc = best
        recovered.append(arc)
        count += 1
        index = end
    return recovered, count


def _chain_points(chain: list[Segment]) -> list[Point]:
    if not chain:
        return []
    return [chain[0].start, *(segment.end for segment in chain)]


def _fit_arc_segment(points: list[Point], tolerance: float) -> Segment | None:
    if len(points) < 4:
        return None
    first = points[0]
    middle = points[len(points) // 2]
    last = points[-1]
    center = _circle_center(first, middle, last)
    if center is None:
        return None
    radius = _distance(first, center)
    if radius <= tolerance:
        return None
    errors = [abs(_distance(point, center) - radius) for point in points]
    if max(errors) > tolerance or sum(errors) / len(errors) > tolerance / 2:
        return None
    signed = _signed_sweep(points, center)
    if abs(signed) < radians(10) or abs(signed) > radians(270):
        return None
    bulge = tan(signed / 4.0)
    return Segment(kind="arc", start=first, end=last, bulge=bulge)


def _circle_center(first: Point, second: Point, third: Point) -> Point | None:
    ax, ay = first
    bx, by = second
    cx, cy = third
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) <= 1e-12:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / determinant
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / determinant
    return ux, uy


def _signed_sweep(points: list[Point], center: Point) -> float:
    total = 0.0
    previous = atan2(points[0][1] - center[1], points[0][0] - center[0])
    for point in points[1:]:
        angle = atan2(point[1] - center[1], point[0] - center[0])
        delta = angle - previous
        while delta <= -pi:
            delta += 2 * pi
        while delta > pi:
            delta -= 2 * pi
        total += delta
        previous = angle
    return total


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
