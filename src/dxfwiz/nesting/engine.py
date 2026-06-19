from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable

import ezdxf
import numpy as np
from ezdxf import path
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from dxfwiz.dxf import clean_dxf, write_geometry_yaml


FLATTENING_DISTANCE = 0.01


@dataclass(frozen=True)
class PartShape:
    name: str
    path: Path
    geometry: Polygon

    @property
    def area(self) -> float:
        return float(self.geometry.area)


@dataclass(frozen=True)
class OrientedPart:
    part_index: int
    rotation: int
    geometry: Polygon
    footprint: Polygon
    width: float
    height: float


@dataclass(frozen=True)
class PlacedPart:
    part_index: int
    part_name: str
    source_path: Path
    x: float
    y: float
    rotation: int
    geometry: Polygon
    footprint: Polygon


@dataclass(frozen=True)
class NestingResult:
    placements: tuple[PlacedPart, ...]
    unplaced: tuple[int, ...]
    stock_width: float
    stock_height: float
    used_width: float
    used_height: float
    spacing: float
    score: float


def load_part_shapes(part_paths: Iterable[str | Path]) -> list[PartShape]:
    return [load_part_shape(path) for path in part_paths]


def load_part_shape(part_path: str | Path) -> PartShape:
    part_path = Path(part_path)
    doc = ezdxf.readfile(part_path)
    polygons = [_entity_polygon(entity) for entity in doc.modelspace()]
    polygons = [polygon for polygon in polygons if polygon is not None and polygon.area > 0]
    if not polygons:
        raise ValueError(f"No closed part geometry found in {part_path}")

    outer = max(polygons, key=lambda polygon: polygon.area)
    holes = [
        polygon
        for polygon in polygons
        if polygon is not outer and outer.buffer(1e-6).covers(polygon)
    ]
    geometry = outer.difference(unary_union(holes)) if holes else outer
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if geometry.is_empty:
        raise ValueError(f"Part geometry collapsed after hole subtraction: {part_path}")
    geometry = _normalize_to_origin(geometry)
    return PartShape(name=part_path.stem, path=part_path, geometry=geometry)


def nest_parts(
    parts: list[PartShape],
    *,
    stock_width: float,
    spacing: float = 0.125,
    rotations: tuple[int, ...] = (0, 90, 180, 270),
    order_keys: np.ndarray | None = None,
    rotation_indices: np.ndarray | None = None,
    compaction_passes: int = 0,
) -> NestingResult:
    if order_keys is None:
        order = sorted(range(len(parts)), key=lambda index: _part_order_key(parts[index]), reverse=True)
    else:
        order = list(np.argsort(np.asarray(order_keys)[: len(parts)]))

    fixed_rotation_indices = rotation_indices is not None
    if fixed_rotation_indices:
        rotation_indices = np.asarray(rotation_indices, dtype=int)

    oriented_cache: dict[tuple[int, int, float], OrientedPart] = {}
    placements: list[PlacedPart] = []
    unplaced: list[int] = []
    stock_height = _initial_stock_height(parts, stock_width)

    for part_index in order:
        candidate_rotations = (
            rotations
            if not fixed_rotation_indices
            else (rotations[int(rotation_indices[part_index]) % len(rotations)],)
        )
        placement = _place_best_rotation(
            parts[part_index],
            part_index,
            candidate_rotations,
            oriented_cache,
            placements,
            stock_width,
            stock_height,
            spacing,
        )
        while placement is None and stock_height < _max_reasonable_height(parts, stock_width):
            stock_height *= 1.35
            placement = _place_best_rotation(
                parts[part_index],
                part_index,
                candidate_rotations,
                oriented_cache,
                placements,
                stock_width,
                stock_height,
                spacing,
            )
        if placement is None:
            unplaced.append(part_index)
        else:
            placements.append(placement)

    if compaction_passes > 0 and not unplaced:
        placements = _compact_placements(
            parts,
            placements,
            rotations,
            oriented_cache,
            stock_width,
            stock_height,
            spacing,
            compaction_passes,
        )

    used_width, used_height = _used_size(placements)
    score = _score(len(unplaced), used_width, used_height, stock_width, stock_height)
    return NestingResult(
        placements=tuple(placements),
        unplaced=tuple(unplaced),
        stock_width=stock_width,
        stock_height=stock_height,
        used_width=used_width,
        used_height=used_height,
        spacing=spacing,
        score=score,
    )


def optimize_nest(
    parts: list[PartShape],
    *,
    stock_width: float,
    spacing: float = 0.125,
    rotations: tuple[int, ...] = (0, 90, 180, 270),
    maxiter: int = 12,
    popsize: int = 6,
    seed: int = 1,
) -> NestingResult:
    from scipy.optimize import differential_evolution

    part_count = len(parts)
    bounds = [(0.0, 1.0)] * part_count + [(0, len(rotations) - 1)] * part_count
    integrality = [False] * part_count + [True] * part_count

    def objective(values) -> float:
        values = np.asarray(values)
        result = nest_parts(
            parts,
            stock_width=stock_width,
            spacing=spacing,
            rotations=rotations,
            order_keys=values[:part_count],
            rotation_indices=values[part_count:],
            compaction_passes=0,
        )
        return result.score

    result = differential_evolution(
        objective,
        bounds,
        integrality=integrality,
        maxiter=maxiter,
        popsize=popsize,
        polish=False,
        seed=seed,
        updating="deferred",
        workers=1,
        tol=0.001,
    )
    values = np.asarray(result.x)
    return nest_parts(
        parts,
        stock_width=stock_width,
        spacing=spacing,
        rotations=rotations,
        order_keys=values[:part_count],
        rotation_indices=values[part_count:],
        compaction_passes=1,
    )


def stock_width_from_source(source_dxf: str | Path) -> float:
    source_dxf = Path(source_dxf)
    with TemporaryDirectory(prefix="dxfwiz_nesting_stock_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        fixed_path = temp_dir / f"{source_dxf.stem}_fixed.dxf"
        geom_path = temp_dir / f"{source_dxf.stem}_geom.yaml"
        clean_dxf(source_dxf, fixed_path)
        geom_data = write_geometry_yaml(
            fixed_path,
            geom_path,
            original_file=source_dxf.name,
            cleaned_file=fixed_path.name,
        )
    entities_by_id = {entity["id"]: entity for entity in geom_data["entities"]}
    populated_frames = [
        node
        for node in geom_data["entity_map"]
        if node["role"] == "frame" and node.get("children")
    ]
    if populated_frames:
        frame = entities_by_id[populated_frames[0]["entity"]]
        bounds = frame["bounding_box"]
    else:
        bounds = geom_data["summary"]["bounding_box"]
    return float(bounds["max"]["x"] - bounds["min"]["x"])


def minimum_stock_width(
    parts: list[PartShape],
    *,
    spacing: float,
    rotations: tuple[int, ...] = (0, 90, 180, 270),
) -> float:
    required = 0.0
    cache: dict[tuple[int, int, float], OrientedPart] = {}
    for index, part in enumerate(parts):
        widths = []
        for rotation in rotations:
            oriented = _oriented_part(part, index, rotation, spacing, cache)
            min_x, _min_y, max_x, _max_y = oriented.footprint.bounds
            widths.append(max_x - min_x)
        required = max(required, min(widths))
    return required


def _place_one(
    oriented: OrientedPart,
    part: PartShape,
    placements: list[PlacedPart],
    stock_width: float,
    stock_height: float,
    spacing: float,
) -> PlacedPart | None:
    stock = box(0, 0, stock_width, stock_height)
    candidates = _candidate_points(
        placements,
        stock_width,
        stock_height,
        oriented.footprint,
        spacing,
    )
    best: tuple[float, ...] | None = None
    best_geometry = None
    best_footprint = None

    for x, y in candidates:
        geometry = affinity.translate(oriented.geometry, xoff=x, yoff=y)
        footprint = affinity.translate(oriented.footprint, xoff=x, yoff=y)
        if not stock.covers(footprint):
            continue
        if any(footprint.intersects(placed.footprint) for placed in placements):
            continue
        score = _candidate_score(placements, geometry, stock_width)
        if best is None or score < best:
            best = score
            best_geometry = geometry
            best_footprint = footprint

    if best_geometry is None or best_footprint is None:
        return None
    min_x, min_y, _max_x, _max_y = best_geometry.bounds
    return PlacedPart(
        part_index=oriented.part_index,
        part_name=part.name,
        source_path=part.path,
        x=float(min_x),
        y=float(min_y),
        rotation=oriented.rotation,
        geometry=best_geometry,
        footprint=best_footprint,
    )


def _place_best_rotation(
    part: PartShape,
    part_index: int,
    rotations: tuple[int, ...],
    oriented_cache: dict[tuple[int, int, float], OrientedPart],
    placements: list[PlacedPart],
    stock_width: float,
    stock_height: float,
    spacing: float,
) -> PlacedPart | None:
    best: tuple[float, ...] | None = None
    best_placement = None
    for rotation in rotations:
        oriented = _oriented_part(part, part_index, rotation, spacing, oriented_cache)
        placement = _place_one(oriented, part, placements, stock_width, stock_height, spacing)
        if placement is None:
            continue
        score = _candidate_score(placements, placement.geometry, stock_width)
        if best is None or score < best:
            best = score
            best_placement = placement
    return best_placement


def _compact_placements(
    parts: list[PartShape],
    placements: list[PlacedPart],
    rotations: tuple[int, ...],
    oriented_cache: dict[tuple[int, int, float], OrientedPart],
    stock_width: float,
    stock_height: float,
    spacing: float,
    passes: int,
) -> list[PlacedPart]:
    compacted = placements[:]
    for _pass in range(passes):
        improved = False
        for placement in sorted(compacted, key=lambda item: (item.geometry.bounds[1], item.geometry.bounds[0])):
            current_score = _layout_score(compacted, stock_width)
            remaining = [item for item in compacted if item is not placement]
            replacement = _place_best_rotation(
                parts[placement.part_index],
                placement.part_index,
                rotations,
                oriented_cache,
                remaining,
                stock_width,
                stock_height,
                spacing,
            )
            if replacement is None:
                continue
            candidate = remaining + [replacement]
            candidate_score = _layout_score(candidate, stock_width)
            if candidate_score < current_score:
                compacted = candidate
                improved = True
        if not improved:
            break
    return compacted


def _layout_score(placements: list[PlacedPart], stock_width: float) -> tuple[float, ...]:
    if not placements:
        return 0.0, 0.0, 0.0
    bounds = [placement.geometry.bounds for placement in placements]
    min_x = min(bound[0] for bound in bounds)
    min_y = min(bound[1] for bound in bounds)
    max_x = max(bound[2] for bound in bounds)
    max_y = max(bound[3] for bound in bounds)
    used_width = max_x - min_x
    used_height = max_y - min_y
    wasted_area = max(
        stock_width * used_height - sum(placement.geometry.area for placement in placements),
        0.0,
    )
    return used_height, used_width, wasted_area


def _candidate_score(
    placements: list[PlacedPart],
    geometry,
    stock_width: float,
) -> tuple[float, ...]:
    bounds = [placement.geometry.bounds for placement in placements] + [geometry.bounds]
    min_x = min(bound[0] for bound in bounds)
    min_y = min(bound[1] for bound in bounds)
    max_x = max(bound[2] for bound in bounds)
    max_y = max(bound[3] for bound in bounds)
    used_width = max_x - min_x
    used_height = max_y - min_y
    wasted_area = max(stock_width * used_height - sum(p.geometry.area for p in placements) - geometry.area, 0.0)
    return (
        used_height,
        used_width,
        wasted_area,
        max_y,
        max_x,
        geometry.bounds[1],
        geometry.bounds[0],
    )


def _candidate_points(
    placements: list[PlacedPart],
    stock_width: float,
    stock_height: float,
    part_footprint,
    spacing: float,
) -> list[tuple[float, float]]:
    part_min_x, part_min_y, part_max_x, part_max_y = part_footprint.bounds
    points = {
        (-part_min_x, -part_min_y),
        (stock_width - part_max_x, -part_min_y),
        (-part_min_x, stock_height - part_max_y),
    }

    anchor_points = _sample_boundary_points(part_footprint, max_points=6)
    x_axes = {-part_min_x, stock_width - part_max_x}
    y_axes = {-part_min_y, stock_height - part_max_y}

    for placed in placements:
        placed_footprint = placed.footprint
        placed_min_x, placed_min_y, placed_max_x, placed_max_y = placed_footprint.bounds
        x_axes.update(
            [
                placed_min_x - part_max_x,
                placed_max_x - part_min_x,
                placed_min_x - part_min_x,
                placed_max_x - part_max_x,
            ]
        )
        y_axes.update(
            [
                placed_min_y - part_max_y,
                placed_max_y - part_min_y,
                placed_min_y - part_min_y,
                placed_max_y - part_max_y,
            ]
        )

        for target_x, target_y in _sample_boundary_points(placed_footprint, max_points=6):
            for anchor_x, anchor_y in anchor_points:
                points.add((target_x - anchor_x, target_y - anchor_y))

    for x in x_axes:
        for y in y_axes:
            points.add((x, y))

    valid_points = []
    for x, y in sorted(_rounded_point(point) for point in points):
        shifted_min_x = part_min_x + x
        shifted_max_x = part_max_x + x
        shifted_min_y = part_min_y + y
        shifted_max_y = part_max_y + y
        if shifted_min_x < -1e-9 or shifted_max_x > stock_width + 1e-9:
            continue
        if shifted_min_y < -1e-9 or shifted_max_y > stock_height + 1e-9:
            continue
        valid_points.append((float(x), float(y)))
    return valid_points


def _sample_boundary_points(geometry, max_points: int) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    geometries = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    for polygon in geometries:
        if polygon.is_empty:
            continue
        coords = [(float(x), float(y)) for x, y in polygon.exterior.coords]
        if not coords:
            continue
        if len(coords) <= max_points:
            points.extend(coords)
        else:
            step = max(1, len(coords) // max_points)
            points.extend(coords[::step][:max_points])
        min_x, min_y, max_x, max_y = polygon.bounds
        points.extend(
            [
                (float(min_x), float(min_y)),
                (float(min_x), float(max_y)),
                (float(max_x), float(min_y)),
                (float(max_x), float(max_y)),
                ((float(min_x) + float(max_x)) / 2, float(min_y)),
                ((float(min_x) + float(max_x)) / 2, float(max_y)),
                (float(min_x), (float(min_y) + float(max_y)) / 2),
                (float(max_x), (float(min_y) + float(max_y)) / 2),
            ]
        )
    return sorted({_rounded_point(point) for point in points})


def _rounded_point(point: tuple[float, float], precision: int = 4) -> tuple[float, float]:
    return round(point[0], precision), round(point[1], precision)


def _oriented_part(
    part: PartShape,
    part_index: int,
    rotation: int,
    spacing: float,
    cache: dict[tuple[int, int, float], OrientedPart],
) -> OrientedPart:
    key = (part_index, rotation, round(spacing, 8))
    if key in cache:
        return cache[key]
    geometry = affinity.rotate(part.geometry, rotation, origin=(0, 0), use_radians=False)
    geometry = _normalize_to_origin(geometry)
    footprint = geometry.buffer(spacing)
    min_x, min_y, max_x, max_y = geometry.bounds
    oriented = OrientedPart(
        part_index=part_index,
        rotation=rotation,
        geometry=geometry,
        footprint=footprint,
        width=float(max_x - min_x),
        height=float(max_y - min_y),
    )
    cache[key] = oriented
    return oriented


def _normalize_to_origin(geometry):
    min_x, min_y, _max_x, _max_y = geometry.bounds
    return affinity.translate(geometry, xoff=-min_x, yoff=-min_y)


def _part_order_key(part: PartShape) -> tuple[float, float, float]:
    min_x, min_y, max_x, max_y = part.geometry.bounds
    width = max_x - min_x
    height = max_y - min_y
    return width * height, max(width, height), part.area


def _initial_stock_height(parts: list[PartShape], stock_width: float) -> float:
    total_area = sum(part.area for part in parts)
    tallest = max(part.geometry.bounds[3] - part.geometry.bounds[1] for part in parts)
    return max(tallest * 1.5, total_area / stock_width * 1.6)


def _max_reasonable_height(parts: list[PartShape], stock_width: float) -> float:
    heights = [part.geometry.bounds[3] - part.geometry.bounds[1] for part in parts]
    return max(sum(heights) * 2.0, stock_width)


def _used_size(placements: list[PlacedPart]) -> tuple[float, float]:
    if not placements:
        return 0.0, 0.0
    bounds = [placement.geometry.bounds for placement in placements]
    return max(bound[2] for bound in bounds), max(bound[3] for bound in bounds)


def _score(
    unplaced_count: int,
    used_width: float,
    used_height: float,
    stock_width: float,
    stock_height: float,
) -> float:
    wasted_area = max(stock_width * max(used_height, 0.0) - used_width * used_height, 0.0)
    return unplaced_count * 1_000_000.0 + used_height * 1_000.0 + used_width + wasted_area / max(stock_height, 1e-9)


def _entity_polygon(entity) -> Polygon | None:
    dxftype = entity.dxftype()
    if dxftype == "CIRCLE":
        center = entity.dxf.center
        return Point(float(center.x), float(center.y)).buffer(float(entity.dxf.radius), quad_segs=48)
    if dxftype == "LWPOLYLINE" and entity.closed:
        coords = [
            (float(vertex.x), float(vertex.y))
            for vertex in path.make_path(entity).flattening(FLATTENING_DISTANCE)
        ]
        if len(coords) < 4:
            return None
        polygon = Polygon(coords)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon if not polygon.is_empty else None
    return None
