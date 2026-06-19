from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
from time import perf_counter
from typing import Literal

import numpy as np

from dxfwiz.schemas.machine import Tool
from dxfwiz.simulation.engine import _arc_points, _depth_from_z, _minimum_tool_diameter, _next_position
from dxfwiz.simulation.model import DexelSimulationRequest, SimulationMetrics, ToolProfile
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove

os.environ.setdefault("TI_CACHE_HOME", str(Path.cwd() / ".tmp" / "taichi_cache"))
os.environ.setdefault("TI_OFFLINE_CACHE", "0")

try:
    import taichi as ti
except ImportError:  # pragma: no cover - exercised in environments without the optional extra.
    ti = None


BackendPreference = Literal["auto", "gpu", "cpu"]

_EXPECTED_CIRCLE = 0
_EXPECTED_RECTANGLE = 1
_EXPECTED_SWEPT_LINE = 2
_EXPECTED_POLYGON = 3
_ACTUAL_CUT = 0
_ACTUAL_RAPID = 1


@dataclass(frozen=True)
class BinnedDexelSettings:
    xy_spacing: float = 0.002
    tile_size: int = 64
    backend: BackendPreference = "auto"
    max_active_cells: int = 80_000_000
    capture_map: bool = False


@dataclass(frozen=True)
class BinnedDexelTimings:
    preprocess_seconds: float
    binning_seconds: float
    kernel_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class BinnedDexelMetrics:
    simulation: SimulationMetrics
    backend: str
    full_grid_width: int
    full_grid_height: int
    active_tiles: int
    active_cells: int
    valid_active_cells: int
    actual_primitives: int
    expected_primitives: int
    primitive_tile_refs: int


@dataclass(frozen=True)
class BinnedDexelMap:
    tile_x: np.ndarray
    tile_y: np.ndarray
    class_codes: np.ndarray
    tile_size: int
    grid_width: int
    grid_height: int


@dataclass(frozen=True)
class BinnedDexelRun:
    metrics: BinnedDexelMetrics
    timings: BinnedDexelTimings
    validation_map: BinnedDexelMap | None = None


@dataclass(frozen=True)
class _ActualPrimitive:
    kind: int
    x0: float
    y0: float
    x1: float
    y1: float
    d0: float
    d1: float
    radius: float
    group_id: int


@dataclass(frozen=True)
class _ExpectedPrimitive:
    kind: int
    x0: float
    y0: float
    x1: float
    y1: float
    d0: float
    d1: float
    radius: float
    vertex_start: int = 0
    vertex_count: int = 0


@dataclass(frozen=True)
class _PrimitiveBatches:
    actual: list[_ActualPrimitive]
    expected: list[_ExpectedPrimitive]
    vertices: list[tuple[float, float]]


@dataclass(frozen=True)
class _TileBatches:
    tile_x: np.ndarray
    tile_y: np.ndarray
    tiles_x: int
    tiles_y: int
    actual_offsets: np.ndarray
    actual_indices: np.ndarray
    expected_offsets: np.ndarray
    expected_indices: np.ndarray
    valid_active_cells: int


def taichi_available() -> bool:
    return ti is not None


def validate_binned_dexels(
    request: DexelSimulationRequest,
    settings: BinnedDexelSettings | None = None,
) -> BinnedDexelRun:
    if ti is None:
        raise RuntimeError("Taichi is not installed. Install it with: python -m pip install taichi")

    total_start = perf_counter()
    settings = settings or BinnedDexelSettings()
    xy_spacing = float(request.settings.xy_spacing or settings.xy_spacing)
    tile_size = int(settings.tile_size)
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")

    preprocess_start = perf_counter()
    tools = {tool.id: tool for tool in request.machine.tools}
    batches = _build_primitive_batches(request, tools, xy_spacing)
    actual_arrays = _actual_arrays(batches.actual)
    expected_arrays = _expected_arrays(batches.expected)
    vertex_arrays = _vertex_arrays(batches.vertices)
    preprocess_seconds = perf_counter() - preprocess_start

    backend_arch = _init_taichi(settings.backend)
    binning_start = perf_counter()
    grid_width, grid_height = _grid_shape(request, xy_spacing)
    if backend_arch in {"cuda", "vulkan", "metal"}:
        tiles = _build_tile_batches_taichi(
            request,
            settings,
            xy_spacing,
            grid_width,
            grid_height,
            actual_arrays,
            expected_arrays,
            batches,
        )
    else:
        tiles = _build_tile_batches_python(request, settings, xy_spacing, grid_width, grid_height, batches)
    binning_seconds = perf_counter() - binning_start
    active_cells = int(len(tiles.tile_x) * tile_size * tile_size)
    if active_cells > settings.max_active_cells:
        raise ValueError(
            f"Binned dexel validation would evaluate {active_cells} active cells, "
            f"above max_active_cells={settings.max_active_cells}"
        )

    kernel_start = perf_counter()
    metrics_i = np.zeros(9, dtype=np.int64)
    metrics_f = np.zeros(2, dtype=np.float32)
    class_codes = np.zeros(active_cells if settings.capture_map else 1, dtype=np.uint8)
    _validate_kernel(
        tiles.tile_x,
        tiles.tile_y,
        tiles.actual_offsets,
        tiles.actual_indices,
        tiles.expected_offsets,
        tiles.expected_indices,
        *actual_arrays,
        *expected_arrays,
        *vertex_arrays,
        class_codes,
        metrics_i,
        metrics_f,
        float(request.stock.bounds.min_x),
        float(request.stock.bounds.min_y),
        float(xy_spacing),
        float(request.stock.thickness),
        int(grid_width),
        int(grid_height),
        int(tile_size),
        int(tile_size * tile_size),
        int(active_cells),
        int(tiles.tiles_x),
        int(settings.capture_map),
    )
    ti.sync()
    kernel_seconds = perf_counter() - kernel_start
    ti.reset()

    simulation_metrics = SimulationMetrics(
        grid_width=grid_width,
        grid_height=grid_height,
        xy_spacing=xy_spacing,
        stock_thickness=float(request.stock.thickness),
        removed_cells=int(metrics_i[1]),
        expected_removed_cells=int(metrics_i[2]),
        overcut_cells=int(metrics_i[3]),
        undercut_cells=int(metrics_i[4]),
        recut_cells=int(metrics_i[5]),
        excessive_recut_cells=int(metrics_i[6]),
        air_cut_moves=0,
        rapid_collision_count=int(metrics_i[8]),
        unsafe_rapid_count=0,
        max_cut_count=int(metrics_i[7]),
        max_actual_depth=float(metrics_f[0]),
        max_expected_depth=float(metrics_f[1]),
    )
    timings = BinnedDexelTimings(
        preprocess_seconds=preprocess_seconds,
        binning_seconds=binning_seconds,
        kernel_seconds=kernel_seconds,
        total_seconds=perf_counter() - total_start,
    )
    metrics = BinnedDexelMetrics(
        simulation=simulation_metrics,
        backend=backend_arch,
        full_grid_width=grid_width,
        full_grid_height=grid_height,
        active_tiles=int(len(tiles.tile_x)),
        active_cells=active_cells,
        valid_active_cells=tiles.valid_active_cells,
        actual_primitives=len(batches.actual),
        expected_primitives=len(batches.expected),
        primitive_tile_refs=int(len(tiles.actual_indices) + len(tiles.expected_indices)),
    )
    validation_map = None
    if settings.capture_map:
        validation_map = BinnedDexelMap(
            tile_x=tiles.tile_x.copy(),
            tile_y=tiles.tile_y.copy(),
            class_codes=class_codes,
            tile_size=tile_size,
            grid_width=grid_width,
            grid_height=grid_height,
        )
    return BinnedDexelRun(metrics=metrics, timings=timings, validation_map=validation_map)


def _build_primitive_batches(
    request: DexelSimulationRequest,
    tools: dict[str, Tool],
    xy_spacing: float,
) -> _PrimitiveBatches:
    actual: list[_ActualPrimitive] = []
    expected: list[_ExpectedPrimitive] = []
    vertices: list[tuple[float, float]] = []

    position = (0.0, 0.0, request.machine.machine.clear_z)
    position = _append_actual_primitives(
        actual,
        request.toolpath_plan.commands,
        position,
        radius=0.0,
        profile=None,
        stock_top_z=request.stock.top_z,
        arc_chord_fraction=request.settings.arc_chord_fraction,
        xy_spacing=xy_spacing,
        group_id=-1,
    )

    for group_id, toolpath_pass in enumerate(request.toolpath_plan.passes):
        tool = tools.get(toolpath_pass.tool or "")
        profile = _tool_profile(toolpath_pass.tool_diameter, tool)
        radius = profile.diameter / 2 if profile is not None else 0.0
        position = _append_actual_primitives(
            actual,
            toolpath_pass.moves,
            position,
            radius=radius,
            profile=profile,
            stock_top_z=request.stock.top_z,
            arc_chord_fraction=request.settings.arc_chord_fraction,
            xy_spacing=xy_spacing,
            group_id=group_id,
        )

    for removal in request.expected_removals:
        if removal.type == "circle":
            expected.append(
                _ExpectedPrimitive(
                    kind=_EXPECTED_CIRCLE,
                    x0=removal.center_x,
                    y0=removal.center_y,
                    x1=0.0,
                    y1=0.0,
                    d0=removal.depth,
                    d1=0.0,
                    radius=removal.radius,
                )
            )
        elif removal.type == "rectangle":
            expected.append(
                _ExpectedPrimitive(
                    kind=_EXPECTED_RECTANGLE,
                    x0=removal.min_x,
                    y0=removal.min_y,
                    x1=removal.max_x,
                    y1=removal.max_y,
                    d0=removal.depth,
                    d1=0.0,
                    radius=0.0,
                )
            )
        elif removal.type == "swept_line":
            expected.append(
                _ExpectedPrimitive(
                    kind=_EXPECTED_SWEPT_LINE,
                    x0=removal.start_x,
                    y0=removal.start_y,
                    x1=removal.end_x,
                    y1=removal.end_y,
                    d0=removal.start_depth,
                    d1=removal.end_depth,
                    radius=removal.radius,
                )
            )
        elif removal.type == "polygon":
            vertex_start = len(vertices)
            vertices.extend((point.x, point.y) for point in removal.points)
            expected.append(
                _ExpectedPrimitive(
                    kind=_EXPECTED_POLYGON,
                    x0=0.0,
                    y0=0.0,
                    x1=0.0,
                    y1=0.0,
                    d0=removal.depth,
                    d1=0.0,
                    radius=0.0,
                    vertex_start=vertex_start,
                    vertex_count=len(removal.points),
                )
            )

    if not actual and not expected:
        _minimum_tool_diameter(request, tools)
    return _PrimitiveBatches(actual=actual, expected=expected, vertices=vertices)


def _append_actual_primitives(
    actual: list[_ActualPrimitive],
    commands,
    position: tuple[float, float, float],
    radius: float,
    profile: ToolProfile | None,
    stock_top_z: float,
    arc_chord_fraction: float,
    xy_spacing: float,
    group_id: int,
) -> tuple[float, float, float]:
    for move in commands:
        if isinstance(move, RapidMove):
            next_position = _next_position(position, move)
            _append_rapid_line(actual, position, next_position, radius, profile, stock_top_z, group_id)
            position = next_position
        elif isinstance(move, LineMove):
            next_position = _next_position(position, move)
            _append_actual_line(actual, position, next_position, radius, profile, stock_top_z, group_id)
            position = next_position
        elif isinstance(move, ArcMove):
            points = _arc_points(position, move, arc_chord_fraction, xy_spacing)
            start = position
            for end in points:
                _append_actual_line(actual, start, end, radius, profile, stock_top_z, group_id)
                start = end
            position = points[-1] if points else _next_position(position, move)
    return position


def _append_actual_line(
    actual: list[_ActualPrimitive],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    profile: ToolProfile | None,
    stock_top_z: float,
    group_id: int,
) -> None:
    if radius <= 0 or profile is None or profile.end_type == "ball":
        return
    start_depth = _depth_from_z(start[2], stock_top_z)
    end_depth = _depth_from_z(end[2], stock_top_z)
    if max(start_depth, end_depth) <= 1e-9:
        return
    actual.append(
        _ActualPrimitive(
            kind=_ACTUAL_CUT,
            x0=start[0],
            y0=start[1],
            x1=end[0],
            y1=end[1],
            d0=start_depth,
            d1=end_depth,
            radius=radius,
            group_id=group_id,
        )
    )


def _append_rapid_line(
    actual: list[_ActualPrimitive],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    profile: ToolProfile | None,
    stock_top_z: float,
    group_id: int,
) -> None:
    if radius <= 0 or profile is None or profile.end_type == "ball":
        return
    start_depth = _depth_from_z(start[2], stock_top_z)
    end_depth = _depth_from_z(end[2], stock_top_z)
    xy_motion = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) > 1e-18
    if not xy_motion and end_depth <= start_depth + 1e-9:
        return
    if max(start_depth, end_depth) <= 1e-9:
        return
    actual.append(
        _ActualPrimitive(
            kind=_ACTUAL_RAPID,
            x0=start[0],
            y0=start[1],
            x1=end[0],
            y1=end[1],
            d0=start_depth,
            d1=end_depth,
            radius=radius,
            group_id=group_id,
        )
    )


def _build_tile_batches_python(
    request: DexelSimulationRequest,
    settings: BinnedDexelSettings,
    xy_spacing: float,
    grid_width: int,
    grid_height: int,
    batches: _PrimitiveBatches,
) -> _TileBatches:
    tile_actual: dict[tuple[int, int], list[int]] = {}
    tile_expected: dict[tuple[int, int], list[int]] = {}
    tile_size = settings.tile_size
    tile_span = xy_spacing * tile_size
    tiles_x, tiles_y = _tile_shape(grid_width, grid_height, tile_size)

    for index, primitive in enumerate(batches.actual):
        for tile in _tiles_for_bounds(request, xy_spacing, tile_span, grid_width, grid_height, _actual_bounds(primitive)):
            tile_actual.setdefault(tile, []).append(index)
    for index, primitive in enumerate(batches.expected):
        for tile in _tiles_for_bounds(
            request,
            xy_spacing,
            tile_span,
            grid_width,
            grid_height,
            _expected_bounds(primitive, batches.vertices),
        ):
            tile_expected.setdefault(tile, []).append(index)

    active_tiles = sorted(set(tile_actual) | set(tile_expected))
    tile_x = np.array([tile[0] for tile in active_tiles], dtype=np.int32)
    tile_y = np.array([tile[1] for tile in active_tiles], dtype=np.int32)
    actual_offsets, actual_indices = _full_tile_csr_indices(tiles_x, tiles_y, tile_actual)
    expected_offsets, expected_indices = _full_tile_csr_indices(tiles_x, tiles_y, tile_expected)
    valid_active_cells = 0
    for x_tile, y_tile in active_tiles:
        valid_width = max(0, min(tile_size, grid_width - x_tile * tile_size))
        valid_height = max(0, min(tile_size, grid_height - y_tile * tile_size))
        valid_active_cells += valid_width * valid_height
    return _TileBatches(
        tile_x=tile_x,
        tile_y=tile_y,
        tiles_x=tiles_x,
        tiles_y=tiles_y,
        actual_offsets=actual_offsets,
        actual_indices=actual_indices,
        expected_offsets=expected_offsets,
        expected_indices=expected_indices,
        valid_active_cells=valid_active_cells,
    )


def _build_tile_batches_taichi(
    request: DexelSimulationRequest,
    settings: BinnedDexelSettings,
    xy_spacing: float,
    grid_width: int,
    grid_height: int,
    actual_arrays: tuple[np.ndarray, ...],
    expected_arrays: tuple[np.ndarray, ...],
    batches: _PrimitiveBatches,
) -> _TileBatches:
    tile_size = settings.tile_size
    tiles_x, tiles_y = _tile_shape(grid_width, grid_height, tile_size)
    total_tiles = tiles_x * tiles_y
    tile_span = float(xy_spacing * tile_size)
    actual_counts = np.zeros(total_tiles, dtype=np.int32)
    expected_counts = np.zeros(total_tiles, dtype=np.int32)
    vertex_arrays = _vertex_arrays(batches.vertices)

    _count_actual_tile_refs(
        *actual_arrays,
        actual_counts,
        float(request.stock.bounds.min_x),
        float(request.stock.bounds.min_y),
        float(request.stock.bounds.max_x),
        float(request.stock.bounds.max_y),
        float(tile_span),
        int(tiles_x),
        int(tiles_y),
    )
    _count_expected_tile_refs(
        *expected_arrays,
        *vertex_arrays,
        expected_counts,
        float(request.stock.bounds.min_x),
        float(request.stock.bounds.min_y),
        float(request.stock.bounds.max_x),
        float(request.stock.bounds.max_y),
        float(tile_span),
        int(tiles_x),
        int(tiles_y),
    )
    ti.sync()

    active_tile_ids = np.nonzero((actual_counts + expected_counts) > 0)[0].astype(np.int32)
    actual_offsets = _offsets_from_counts(actual_counts)
    expected_offsets = _offsets_from_counts(expected_counts)
    actual_indices = np.empty(int(actual_offsets[-1]), dtype=np.int32)
    expected_indices = np.empty(int(expected_offsets[-1]), dtype=np.int32)
    actual_cursors = np.zeros(total_tiles, dtype=np.int32)
    expected_cursors = np.zeros(total_tiles, dtype=np.int32)

    _fill_actual_tile_refs(
        *actual_arrays,
        actual_offsets,
        actual_cursors,
        actual_indices,
        float(request.stock.bounds.min_x),
        float(request.stock.bounds.min_y),
        float(request.stock.bounds.max_x),
        float(request.stock.bounds.max_y),
        float(tile_span),
        int(tiles_x),
        int(tiles_y),
    )
    _fill_expected_tile_refs(
        *expected_arrays,
        *vertex_arrays,
        expected_offsets,
        expected_cursors,
        expected_indices,
        float(request.stock.bounds.min_x),
        float(request.stock.bounds.min_y),
        float(request.stock.bounds.max_x),
        float(request.stock.bounds.max_y),
        float(tile_span),
        int(tiles_x),
        int(tiles_y),
    )
    ti.sync()

    tile_x = (active_tile_ids % tiles_x).astype(np.int32)
    tile_y = (active_tile_ids // tiles_x).astype(np.int32)
    _sort_tile_indices(active_tile_ids, actual_offsets, actual_indices)
    _sort_tile_indices(active_tile_ids, expected_offsets, expected_indices)
    valid_widths = np.maximum(0, np.minimum(tile_size, grid_width - tile_x * tile_size))
    valid_heights = np.maximum(0, np.minimum(tile_size, grid_height - tile_y * tile_size))
    return _TileBatches(
        tile_x=tile_x,
        tile_y=tile_y,
        tiles_x=tiles_x,
        tiles_y=tiles_y,
        actual_offsets=actual_offsets,
        actual_indices=actual_indices,
        expected_offsets=expected_offsets,
        expected_indices=expected_indices,
        valid_active_cells=int(np.sum(valid_widths * valid_heights)),
    )


def _offsets_from_counts(counts: np.ndarray) -> np.ndarray:
    offsets = np.empty(len(counts) + 1, dtype=np.int32)
    offsets[0] = 0
    offsets[1:] = np.cumsum(counts, dtype=np.int64).astype(np.int32)
    return offsets


def _sort_tile_indices(tile_ids: np.ndarray, offsets: np.ndarray, indices: np.ndarray) -> None:
    for tile_id in tile_ids:
        start = int(offsets[tile_id])
        end = int(offsets[tile_id + 1])
        if end - start > 1:
            indices[start:end].sort()


def _tiles_for_bounds(
    request: DexelSimulationRequest,
    xy_spacing: float,
    tile_span: float,
    grid_width: int,
    grid_height: int,
    bounds: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    min_x, min_y, max_x, max_y = bounds
    stock = request.stock.bounds
    if max_x < stock.min_x or max_y < stock.min_y or min_x > stock.max_x or min_y > stock.max_y:
        return []
    max_tile_x = max(0, (grid_width - 1) // int(round(tile_span / xy_spacing)))
    max_tile_y = max(0, (grid_height - 1) // int(round(tile_span / xy_spacing)))
    x0 = max(0, int(np.floor((min_x - stock.min_x) / tile_span)))
    y0 = max(0, int(np.floor((min_y - stock.min_y) / tile_span)))
    x1 = min(max_tile_x, int(np.floor((max_x - stock.min_x) / tile_span)))
    y1 = min(max_tile_y, int(np.floor((max_y - stock.min_y) / tile_span)))
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def _actual_bounds(primitive: _ActualPrimitive) -> tuple[float, float, float, float]:
    return (
        min(primitive.x0, primitive.x1) - primitive.radius,
        min(primitive.y0, primitive.y1) - primitive.radius,
        max(primitive.x0, primitive.x1) + primitive.radius,
        max(primitive.y0, primitive.y1) + primitive.radius,
    )


def _expected_bounds(
    primitive: _ExpectedPrimitive,
    vertices: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    if primitive.kind == _EXPECTED_CIRCLE:
        return (
            primitive.x0 - primitive.radius,
            primitive.y0 - primitive.radius,
            primitive.x0 + primitive.radius,
            primitive.y0 + primitive.radius,
        )
    if primitive.kind == _EXPECTED_RECTANGLE:
        return (primitive.x0, primitive.y0, primitive.x1, primitive.y1)
    if primitive.kind == _EXPECTED_SWEPT_LINE:
        return (
            min(primitive.x0, primitive.x1) - primitive.radius,
            min(primitive.y0, primitive.y1) - primitive.radius,
            max(primitive.x0, primitive.x1) + primitive.radius,
            max(primitive.y0, primitive.y1) + primitive.radius,
        )
    polygon = vertices[primitive.vertex_start : primitive.vertex_start + primitive.vertex_count]
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _full_tile_csr_indices(
    tiles_x: int,
    tiles_y: int,
    tile_indices: dict[tuple[int, int], list[int]],
) -> tuple[np.ndarray, np.ndarray]:
    offsets = [0]
    indices: list[int] = []
    for tile_y in range(tiles_y):
        for tile_x in range(tiles_x):
            indices.extend(tile_indices.get((tile_x, tile_y), []))
            offsets.append(len(indices))
    return np.array(offsets, dtype=np.int32), np.array(indices, dtype=np.int32)


def _tile_shape(grid_width: int, grid_height: int, tile_size: int) -> tuple[int, int]:
    return (
        max(1, int(np.ceil(grid_width / tile_size))),
        max(1, int(np.ceil(grid_height / tile_size))),
    )


def _grid_shape(request: DexelSimulationRequest, xy_spacing: float) -> tuple[int, int]:
    bounds = request.stock.bounds
    width = max(1, int(np.ceil((bounds.max_x - bounds.min_x) / xy_spacing)))
    height = max(1, int(np.ceil((bounds.max_y - bounds.min_y) / xy_spacing)))
    return width, height


def _tool_profile(pass_diameter: float | None, tool: Tool | None) -> ToolProfile | None:
    diameter = pass_diameter or (tool.diameter if tool is not None else None)
    if diameter is None:
        return None
    return ToolProfile(diameter=diameter, end_type=tool.end_type if tool is not None else "flat")


def _actual_arrays(actual: list[_ActualPrimitive]) -> tuple[np.ndarray, ...]:
    return (
        np.array([item.kind for item in actual], dtype=np.int32),
        np.array([item.x0 for item in actual], dtype=np.float32),
        np.array([item.y0 for item in actual], dtype=np.float32),
        np.array([item.x1 for item in actual], dtype=np.float32),
        np.array([item.y1 for item in actual], dtype=np.float32),
        np.array([item.d0 for item in actual], dtype=np.float32),
        np.array([item.d1 for item in actual], dtype=np.float32),
        np.array([item.radius for item in actual], dtype=np.float32),
        np.array([item.group_id for item in actual], dtype=np.int32),
    )


def _expected_arrays(expected: list[_ExpectedPrimitive]) -> tuple[np.ndarray, ...]:
    return (
        np.array([item.kind for item in expected], dtype=np.int32),
        np.array([item.x0 for item in expected], dtype=np.float32),
        np.array([item.y0 for item in expected], dtype=np.float32),
        np.array([item.x1 for item in expected], dtype=np.float32),
        np.array([item.y1 for item in expected], dtype=np.float32),
        np.array([item.d0 for item in expected], dtype=np.float32),
        np.array([item.d1 for item in expected], dtype=np.float32),
        np.array([item.radius for item in expected], dtype=np.float32),
        np.array([item.vertex_start for item in expected], dtype=np.int32),
        np.array([item.vertex_count for item in expected], dtype=np.int32),
    )


def _vertex_arrays(vertices: list[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([point[0] for point in vertices], dtype=np.float32),
        np.array([point[1] for point in vertices], dtype=np.float32),
    )


def _init_taichi(backend: BackendPreference) -> str:
    if ti is None:
        raise RuntimeError("Taichi is not installed")
    cache_path = str(Path.cwd() / ".tmp" / "taichi_cache")
    init_kwargs = {"offline_cache": False, "offline_cache_file_path": cache_path}
    if backend == "cpu":
        ti.init(arch=ti.cpu, **init_kwargs)
    else:
        try:
            ti.init(arch=ti.gpu, **init_kwargs)
        except Exception:
            if backend == "gpu":
                raise
            ti.init(arch=ti.cpu, **init_kwargs)
    return str(ti.lang.impl.current_cfg().arch).replace("Arch.", "")


def render_binned_dexel_map_png(
    run: BinnedDexelRun,
    max_dimension: int | None = 4096,
    include_legend: bool = True,
    mode: Literal["correctness", "diagnostic"] = "correctness",
) -> bytes:
    class_image = binned_dexel_class_image(run, max_dimension=max_dimension)
    display_image, palette = _display_classes(class_image, mode)
    rgb = palette[display_image]
    from PIL import Image, ImageDraw

    image = Image.fromarray(rgb, mode="RGB")
    if include_legend:
        image = _with_validation_legend(image, run, class_image.shape[1], class_image.shape[0], mode)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def binned_dexel_class_image(run: BinnedDexelRun, max_dimension: int | None = 4096) -> np.ndarray:
    if run.validation_map is None:
        raise ValueError("validate_binned_dexels must be called with capture_map=True")
    validation_map = run.validation_map
    if max_dimension is not None and max_dimension <= 0:
        raise ValueError("max_dimension must be positive or None")

    scale = 1.0
    if max_dimension is not None:
        scale = min(1.0, max_dimension / max(validation_map.grid_width, validation_map.grid_height))
    output_width = max(1, int(np.ceil(validation_map.grid_width * scale)))
    output_height = max(1, int(np.ceil(validation_map.grid_height * scale)))
    class_image = np.zeros((output_height, output_width), dtype=np.uint8)
    tile_cell_count = validation_map.tile_size * validation_map.tile_size

    for tile_index, (tile_x, tile_y) in enumerate(zip(validation_map.tile_x, validation_map.tile_y, strict=True)):
        start = tile_index * tile_cell_count
        tile = validation_map.class_codes[start : start + tile_cell_count].reshape(
            validation_map.tile_size,
            validation_map.tile_size,
        )
        source_x = int(tile_x) * validation_map.tile_size
        source_y = int(tile_y) * validation_map.tile_size
        valid_width = max(0, min(validation_map.tile_size, validation_map.grid_width - source_x))
        valid_height = max(0, min(validation_map.tile_size, validation_map.grid_height - source_y))
        if valid_width == 0 or valid_height == 0:
            continue
        tile = tile[:valid_height, :valid_width]
        if scale == 1.0:
            dest_x = source_x
            dest_y = output_height - source_y - valid_height
            target = class_image[dest_y : dest_y + valid_height, dest_x : dest_x + valid_width]
            np.maximum(target, tile[::-1, :], out=target)
            continue

        rows, cols = np.nonzero(tile)
        if rows.size == 0:
            continue
        codes = tile[rows, cols]
        out_x = np.minimum(((source_x + cols) * scale).astype(np.int32), output_width - 1)
        out_y = output_height - 1 - np.minimum(((source_y + rows) * scale).astype(np.int32), output_height - 1)
        np.maximum.at(class_image, (out_y, out_x), codes)
    return class_image


def _display_classes(
    class_image: np.ndarray,
    mode: Literal["correctness", "diagnostic"],
) -> tuple[np.ndarray, np.ndarray]:
    palette = np.array(
        [
            [255, 255, 255],  # not evaluated
            [229, 235, 235],  # evaluated, no cut
            [22, 163, 74],  # correct cut
            [22, 163, 74],  # acceptable recut
            [245, 158, 11],  # excessive recut
            [249, 115, 22],  # undercut
            [220, 38, 38],  # overcut
            [127, 29, 29],  # rapid collision
        ],
        dtype=np.uint8,
    )
    return class_image, palette


def _with_validation_legend(image, run: BinnedDexelRun, map_width: int, map_height: int, mode: str):
    from PIL import Image, ImageDraw

    legend_width = 300
    padding = 14
    canvas = Image.new("RGB", (map_width + legend_width, max(map_height, 220)), "white")
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    x = map_width + padding
    y = padding
    draw.text((x, y), "Binned dexel map", fill=(17, 24, 39))
    y += 24
    draw.text((x, y), f"spacing: {run.metrics.simulation.xy_spacing:.4f} in", fill=(55, 65, 81))
    y += 18
    draw.text((x, y), f"backend: {run.metrics.backend}", fill=(55, 65, 81))
    y += 18
    draw.text((x, y), f"active cells: {run.metrics.valid_active_cells:,}", fill=(55, 65, 81))
    y += 28
    if mode == "correctness":
        entries = [
            ((229, 235, 235), "evaluated, no cut"),
            ((22, 163, 74), "correct"),
            ((245, 158, 11), "excessive recut"),
            ((249, 115, 22), "undercut"),
            ((220, 38, 38), "overcut"),
            ((127, 29, 29), "rapid collision"),
            ((255, 255, 255), "not evaluated"),
        ]
    else:
        entries = [
            ((229, 235, 235), "evaluated, no cut"),
            ((22, 163, 74), "acceptable cut"),
            ((245, 158, 11), "excessive recut"),
            ((249, 115, 22), "undercut"),
            ((220, 38, 38), "overcut"),
            ((127, 29, 29), "rapid collision"),
            ((255, 255, 255), "not evaluated"),
        ]
    for color, label in entries:
        draw.rectangle((x, y, x + 14, y + 14), fill=color, outline=(148, 163, 184))
        draw.text((x + 22, y - 1), label, fill=(31, 41, 55))
        y += 22
    return canvas


if ti is not None:

    @ti.func
    def _tile_range_from_bounds(
        bounds_min_x: ti.f32,
        bounds_min_y: ti.f32,
        bounds_max_x: ti.f32,
        bounds_max_y: ti.f32,
        stock_min_x: ti.f32,
        stock_min_y: ti.f32,
        stock_max_x: ti.f32,
        stock_max_y: ti.f32,
        tile_span: ti.f32,
        tiles_x: ti.i32,
        tiles_y: ti.i32,
    ):
        active = bounds_max_x >= stock_min_x and bounds_max_y >= stock_min_y and bounds_min_x <= stock_max_x and bounds_min_y <= stock_max_y
        x0 = ti.cast(0, ti.i32)
        y0 = ti.cast(0, ti.i32)
        x1 = ti.cast(-1, ti.i32)
        y1 = ti.cast(-1, ti.i32)
        if active:
            x0 = ti.max(0, ti.cast(ti.floor((bounds_min_x - stock_min_x) / tile_span), ti.i32))
            y0 = ti.max(0, ti.cast(ti.floor((bounds_min_y - stock_min_y) / tile_span), ti.i32))
            x1 = ti.min(tiles_x - 1, ti.cast(ti.floor((bounds_max_x - stock_min_x) / tile_span), ti.i32))
            y1 = ti.min(tiles_y - 1, ti.cast(ti.floor((bounds_max_y - stock_min_y) / tile_span), ti.i32))
            active = x0 <= x1 and y0 <= y1
        return active, x0, y0, x1, y1

    @ti.kernel
    def _count_actual_tile_refs(
        akind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ax0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ax1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ar: ti.types.ndarray(dtype=ti.f32, ndim=1),
        agroup: ti.types.ndarray(dtype=ti.i32, ndim=1),
        counts: ti.types.ndarray(dtype=ti.i32, ndim=1),
        stock_min_x: ti.f32,
        stock_min_y: ti.f32,
        stock_max_x: ti.f32,
        stock_max_y: ti.f32,
        tile_span: ti.f32,
        tiles_x: ti.i32,
        tiles_y: ti.i32,
    ):
        for primitive in range(ax0.shape[0]):
            active, x0, y0, x1, y1 = _tile_range_from_bounds(
                ti.min(ax0[primitive], ax1[primitive]) - ar[primitive],
                ti.min(ay0[primitive], ay1[primitive]) - ar[primitive],
                ti.max(ax0[primitive], ax1[primitive]) + ar[primitive],
                ti.max(ay0[primitive], ay1[primitive]) + ar[primitive],
                stock_min_x,
                stock_min_y,
                stock_max_x,
                stock_max_y,
                tile_span,
                tiles_x,
                tiles_y,
            )
            if active:
                for ty in range(y0, y1 + 1):
                    for tx in range(x0, x1 + 1):
                        ti.atomic_add(counts[ty * tiles_x + tx], 1)

    @ti.kernel
    def _fill_actual_tile_refs(
        akind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ax0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ax1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ar: ti.types.ndarray(dtype=ti.f32, ndim=1),
        agroup: ti.types.ndarray(dtype=ti.i32, ndim=1),
        offsets: ti.types.ndarray(dtype=ti.i32, ndim=1),
        cursors: ti.types.ndarray(dtype=ti.i32, ndim=1),
        indices: ti.types.ndarray(dtype=ti.i32, ndim=1),
        stock_min_x: ti.f32,
        stock_min_y: ti.f32,
        stock_max_x: ti.f32,
        stock_max_y: ti.f32,
        tile_span: ti.f32,
        tiles_x: ti.i32,
        tiles_y: ti.i32,
    ):
        for primitive in range(ax0.shape[0]):
            active, x0, y0, x1, y1 = _tile_range_from_bounds(
                ti.min(ax0[primitive], ax1[primitive]) - ar[primitive],
                ti.min(ay0[primitive], ay1[primitive]) - ar[primitive],
                ti.max(ax0[primitive], ax1[primitive]) + ar[primitive],
                ti.max(ay0[primitive], ay1[primitive]) + ar[primitive],
                stock_min_x,
                stock_min_y,
                stock_max_x,
                stock_max_y,
                tile_span,
                tiles_x,
                tiles_y,
            )
            if active:
                for ty in range(y0, y1 + 1):
                    for tx in range(x0, x1 + 1):
                        tile_id = ty * tiles_x + tx
                        cursor = ti.atomic_add(cursors[tile_id], 1)
                        indices[offsets[tile_id] + cursor] = primitive

    @ti.func
    def _expected_bounds_for_primitive(
        primitive: ti.i32,
        ekind: ti.types.ndarray(),
        ex0: ti.types.ndarray(),
        ey0: ti.types.ndarray(),
        ex1: ti.types.ndarray(),
        ey1: ti.types.ndarray(),
        er: ti.types.ndarray(),
        evstart: ti.types.ndarray(),
        evcount: ti.types.ndarray(),
        evx: ti.types.ndarray(),
        evy: ti.types.ndarray(),
    ):
        kind = ekind[primitive]
        min_x = ex0[primitive]
        min_y = ey0[primitive]
        max_x = ex1[primitive]
        max_y = ey1[primitive]
        if kind == 0:
            min_x = ex0[primitive] - er[primitive]
            min_y = ey0[primitive] - er[primitive]
            max_x = ex0[primitive] + er[primitive]
            max_y = ey0[primitive] + er[primitive]
        elif kind == 2:
            min_x = ti.min(ex0[primitive], ex1[primitive]) - er[primitive]
            min_y = ti.min(ey0[primitive], ey1[primitive]) - er[primitive]
            max_x = ti.max(ex0[primitive], ex1[primitive]) + er[primitive]
            max_y = ti.max(ey0[primitive], ey1[primitive]) + er[primitive]
        elif kind == 3:
            vertex_start = evstart[primitive]
            vertex_count = evcount[primitive]
            min_x = ti.cast(1.0e20, ti.f32)
            min_y = ti.cast(1.0e20, ti.f32)
            max_x = ti.cast(-1.0e20, ti.f32)
            max_y = ti.cast(-1.0e20, ti.f32)
            for vertex in range(vertex_count):
                x = evx[vertex_start + vertex]
                y = evy[vertex_start + vertex]
                min_x = ti.min(min_x, x)
                min_y = ti.min(min_y, y)
                max_x = ti.max(max_x, x)
                max_y = ti.max(max_y, y)
        return min_x, min_y, max_x, max_y

    @ti.kernel
    def _count_expected_tile_refs(
        ekind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ex0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ex1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        er: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evstart: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evcount: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evx: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evy: ti.types.ndarray(dtype=ti.f32, ndim=1),
        counts: ti.types.ndarray(dtype=ti.i32, ndim=1),
        stock_min_x: ti.f32,
        stock_min_y: ti.f32,
        stock_max_x: ti.f32,
        stock_max_y: ti.f32,
        tile_span: ti.f32,
        tiles_x: ti.i32,
        tiles_y: ti.i32,
    ):
        for primitive in range(ekind.shape[0]):
            min_x, min_y, max_x, max_y = _expected_bounds_for_primitive(
                primitive,
                ekind,
                ex0,
                ey0,
                ex1,
                ey1,
                er,
                evstart,
                evcount,
                evx,
                evy,
            )
            active, x0, y0, x1, y1 = _tile_range_from_bounds(
                min_x,
                min_y,
                max_x,
                max_y,
                stock_min_x,
                stock_min_y,
                stock_max_x,
                stock_max_y,
                tile_span,
                tiles_x,
                tiles_y,
            )
            if active:
                for ty in range(y0, y1 + 1):
                    for tx in range(x0, x1 + 1):
                        ti.atomic_add(counts[ty * tiles_x + tx], 1)

    @ti.kernel
    def _fill_expected_tile_refs(
        ekind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ex0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ex1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        er: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evstart: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evcount: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evx: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evy: ti.types.ndarray(dtype=ti.f32, ndim=1),
        offsets: ti.types.ndarray(dtype=ti.i32, ndim=1),
        cursors: ti.types.ndarray(dtype=ti.i32, ndim=1),
        indices: ti.types.ndarray(dtype=ti.i32, ndim=1),
        stock_min_x: ti.f32,
        stock_min_y: ti.f32,
        stock_max_x: ti.f32,
        stock_max_y: ti.f32,
        tile_span: ti.f32,
        tiles_x: ti.i32,
        tiles_y: ti.i32,
    ):
        for primitive in range(ekind.shape[0]):
            min_x, min_y, max_x, max_y = _expected_bounds_for_primitive(
                primitive,
                ekind,
                ex0,
                ey0,
                ex1,
                ey1,
                er,
                evstart,
                evcount,
                evx,
                evy,
            )
            active, x0, y0, x1, y1 = _tile_range_from_bounds(
                min_x,
                min_y,
                max_x,
                max_y,
                stock_min_x,
                stock_min_y,
                stock_max_x,
                stock_max_y,
                tile_span,
                tiles_x,
                tiles_y,
            )
            if active:
                for ty in range(y0, y1 + 1):
                    for tx in range(x0, x1 + 1):
                        tile_id = ty * tiles_x + tx
                        cursor = ti.atomic_add(cursors[tile_id], 1)
                        indices[offsets[tile_id] + cursor] = primitive

    @ti.kernel
    def _validate_kernel(
        tile_x: ti.types.ndarray(dtype=ti.i32, ndim=1),
        tile_y: ti.types.ndarray(dtype=ti.i32, ndim=1),
        actual_offsets: ti.types.ndarray(dtype=ti.i32, ndim=1),
        actual_indices: ti.types.ndarray(dtype=ti.i32, ndim=1),
        expected_offsets: ti.types.ndarray(dtype=ti.i32, ndim=1),
        expected_indices: ti.types.ndarray(dtype=ti.i32, ndim=1),
        akind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ax0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ax1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ay1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ad1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ar: ti.types.ndarray(dtype=ti.f32, ndim=1),
        agroup: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ekind: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ex0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ex1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ey1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed0: ti.types.ndarray(dtype=ti.f32, ndim=1),
        ed1: ti.types.ndarray(dtype=ti.f32, ndim=1),
        er: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evstart: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evcount: ti.types.ndarray(dtype=ti.i32, ndim=1),
        evx: ti.types.ndarray(dtype=ti.f32, ndim=1),
        evy: ti.types.ndarray(dtype=ti.f32, ndim=1),
        class_codes: ti.types.ndarray(dtype=ti.u8, ndim=1),
        metrics_i: ti.types.ndarray(dtype=ti.i64, ndim=1),
        metrics_f: ti.types.ndarray(dtype=ti.f32, ndim=1),
        min_x: ti.f32,
        min_y: ti.f32,
        spacing: ti.f32,
        stock_thickness: ti.f32,
        grid_width: ti.i32,
        grid_height: ti.i32,
        tile_size: ti.i32,
        tile_cell_count: ti.i32,
        active_cells: ti.i32,
        tiles_x: ti.i32,
        capture_map: ti.i32,
    ):
        tolerance = ti.max(spacing * 0.1, 1.0e-6)
        for cell in range(active_cells):
            tile_index = cell // tile_cell_count
            local = cell - tile_index * tile_cell_count
            local_x = local % tile_size
            local_y = local // tile_size
            grid_x = tile_x[tile_index] * tile_size + local_x
            grid_y = tile_y[tile_index] * tile_size + local_y
            if grid_x < grid_width and grid_y < grid_height:
                tile_id = tile_y[tile_index] * tiles_x + tile_x[tile_index]
                x = min_x + (ti.cast(grid_x, ti.f32) + 0.5) * spacing
                y = min_y + (ti.cast(grid_y, ti.f32) + 0.5) * spacing
                actual_depth = ti.cast(0.0, ti.f32)
                expected_depth = ti.cast(0.0, ti.f32)
                cut_count = ti.cast(0, ti.i32)
                last_cut_group = ti.cast(-2147483648, ti.i32)
                rapid_collision = False

                actual_start = actual_offsets[tile_id]
                actual_end = actual_offsets[tile_id + 1]
                for cursor in range(actual_start, actual_end):
                    primitive = actual_indices[cursor]
                    dx = ax1[primitive] - ax0[primitive]
                    dy = ay1[primitive] - ay0[primitive]
                    length_sq = dx * dx + dy * dy
                    t = ti.cast(0.0, ti.f32)
                    if length_sq > 1.0e-12:
                        t = ((x - ax0[primitive]) * dx + (y - ay0[primitive]) * dy) / length_sq
                        t = ti.min(ti.max(t, 0.0), 1.0)
                    nearest_x = ax0[primitive] + t * dx
                    nearest_y = ay0[primitive] + t * dy
                    radial_sq = (x - nearest_x) * (x - nearest_x) + (y - nearest_y) * (y - nearest_y)
                    if radial_sq <= ar[primitive] * ar[primitive]:
                        depth = ad0[primitive] + t * (ad1[primitive] - ad0[primitive])
                        if length_sq <= 1.0e-12:
                            depth = ti.max(ad0[primitive], ad1[primitive])
                        depth = ti.min(depth, stock_thickness)
                        if akind[primitive] == 1:
                            if depth > actual_depth + tolerance:
                                rapid_collision = True
                        else:
                            group_id = agroup[primitive]
                            if depth > 1.0e-9 and actual_depth < stock_thickness - 1.0e-6 and group_id != last_cut_group:
                                cut_count += 1
                                last_cut_group = group_id
                            if depth > actual_depth:
                                actual_depth = depth

                expected_start = expected_offsets[tile_id]
                expected_end = expected_offsets[tile_id + 1]
                for cursor in range(expected_start, expected_end):
                    primitive = expected_indices[cursor]
                    kind = ekind[primitive]
                    inside = False
                    depth = ed0[primitive]
                    if kind == 0:
                        radial_sq = (x - ex0[primitive]) * (x - ex0[primitive]) + (y - ey0[primitive]) * (y - ey0[primitive])
                        inside = radial_sq <= er[primitive] * er[primitive]
                    elif kind == 1:
                        inside = x >= ex0[primitive] and x <= ex1[primitive] and y >= ey0[primitive] and y <= ey1[primitive]
                    elif kind == 2:
                        dx = ex1[primitive] - ex0[primitive]
                        dy = ey1[primitive] - ey0[primitive]
                        length_sq = dx * dx + dy * dy
                        t = ti.cast(0.0, ti.f32)
                        if length_sq > 1.0e-12:
                            t = ((x - ex0[primitive]) * dx + (y - ey0[primitive]) * dy) / length_sq
                            t = ti.min(ti.max(t, 0.0), 1.0)
                        nearest_x = ex0[primitive] + t * dx
                        nearest_y = ey0[primitive] + t * dy
                        radial_sq = (x - nearest_x) * (x - nearest_x) + (y - nearest_y) * (y - nearest_y)
                        inside = radial_sq <= er[primitive] * er[primitive]
                        depth = ed0[primitive] + t * (ed1[primitive] - ed0[primitive])
                        if length_sq <= 1.0e-12:
                            depth = ti.max(ed0[primitive], ed1[primitive])
                    else:
                        vertex_start = evstart[primitive]
                        vertex_count = evcount[primitive]
                        previous = vertex_count - 1
                        for vertex in range(vertex_count):
                            xi = evx[vertex_start + vertex]
                            yi = evy[vertex_start + vertex]
                            xj = evx[vertex_start + previous]
                            yj = evy[vertex_start + previous]
                            crosses = (yi > y) != (yj > y)
                            if crosses and x < (xj - xi) * (y - yi) / (yj - yi + 1.0e-20) + xi:
                                inside = not inside
                            previous = vertex
                    if inside:
                        depth = ti.min(depth, stock_thickness)
                        if depth > expected_depth:
                            expected_depth = depth

                ti.atomic_add(metrics_i[0], 1)
                if actual_depth > tolerance:
                    ti.atomic_add(metrics_i[1], 1)
                if expected_depth > tolerance:
                    ti.atomic_add(metrics_i[2], 1)
                if actual_depth > expected_depth + tolerance:
                    ti.atomic_add(metrics_i[3], 1)
                if expected_depth > actual_depth + tolerance:
                    ti.atomic_add(metrics_i[4], 1)
                if cut_count > 1:
                    ti.atomic_add(metrics_i[5], 1)
                if cut_count >= 4:
                    ti.atomic_add(metrics_i[6], 1)
                ti.atomic_max(metrics_i[7], cut_count)
                if rapid_collision:
                    ti.atomic_add(metrics_i[8], 1)
                ti.atomic_max(metrics_f[0], actual_depth)
                ti.atomic_max(metrics_f[1], expected_depth)
                if capture_map != 0:
                    code = ti.cast(1, ti.u8)
                    if actual_depth > tolerance or expected_depth > tolerance:
                        code = ti.cast(2, ti.u8)
                    if cut_count > 1:
                        code = ti.cast(3, ti.u8)
                    if cut_count >= 4:
                        code = ti.cast(4, ti.u8)
                    if expected_depth > actual_depth + tolerance:
                        code = ti.cast(5, ti.u8)
                    if actual_depth > expected_depth + tolerance:
                        code = ti.cast(6, ti.u8)
                    if rapid_collision:
                        code = ti.cast(7, ti.u8)
                    class_codes[cell] = code
