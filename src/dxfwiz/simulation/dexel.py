from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely import contains_xy
from shapely.geometry import Polygon

from dxfwiz.simulation.model import (
    SimulationBounds,
    SimulationSettings,
    SimulationSnapshot,
    SimulationStock,
    ToolProfile,
)


@dataclass
class DexelGrid:
    stock: SimulationStock
    xy_spacing: float
    x_values: np.ndarray
    y_values: np.ndarray
    actual_depth: np.ndarray
    expected_depth: np.ndarray
    cut_count: np.ndarray
    last_operation_id: np.ndarray
    operation_lookup: dict[int, str] = field(default_factory=dict)
    _operation_ids: dict[str, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        stock: SimulationStock,
        settings: SimulationSettings,
        minimum_tool_diameter: float,
    ) -> "DexelGrid":
        xy_spacing = settings.xy_spacing or minimum_tool_diameter * settings.xy_tool_fraction
        bounds = stock.bounds
        x_values = _axis_centers(bounds.min_x, bounds.max_x, xy_spacing)
        y_values = _axis_centers(bounds.min_y, bounds.max_y, xy_spacing)
        cell_count = len(x_values) * len(y_values)
        if cell_count > settings.max_grid_cells:
            raise ValueError(
                f"Dexel grid would have {cell_count} cells, above max_grid_cells={settings.max_grid_cells}"
            )
        shape = (len(y_values), len(x_values))
        return cls(
            stock=stock,
            xy_spacing=xy_spacing,
            x_values=x_values,
            y_values=y_values,
            actual_depth=np.zeros(shape, dtype=np.float32),
            expected_depth=np.zeros(shape, dtype=np.float32),
            cut_count=np.zeros(shape, dtype=np.uint16),
            last_operation_id=np.full(shape, -1, dtype=np.int32),
        )

    @property
    def width(self) -> int:
        return int(self.actual_depth.shape[1])

    @property
    def height(self) -> int:
        return int(self.actual_depth.shape[0])

    @property
    def bounds(self) -> SimulationBounds:
        return self.stock.bounds

    def operation_index(self, operation_id: str) -> int:
        if operation_id not in self._operation_ids:
            index = len(self._operation_ids)
            self._operation_ids[operation_id] = index
            self.operation_lookup[index] = operation_id
        return self._operation_ids[operation_id]

    def expected_circle(self, center: tuple[float, float], radius: float, depth: float) -> None:
        depth = self._expected_depth(depth)
        yy, xx = self._window_mesh(center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
        if xx.size == 0:
            return
        x_slice, y_slice = self._window_slices(center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
        mask = (xx - center[0]) ** 2 + (yy - center[1]) ** 2 <= radius**2
        target = self.expected_depth[y_slice, x_slice]
        np.maximum(target, depth, out=target, where=mask)

    def expected_rectangle(self, min_x: float, min_y: float, max_x: float, max_y: float, depth: float) -> None:
        depth = self._expected_depth(depth)
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        if _empty_slice(x_slice) or _empty_slice(y_slice):
            return
        target = self.expected_depth[y_slice, x_slice]
        np.maximum(target, depth, out=target)

    def expected_polygon(self, points: list[tuple[float, float]], depth: float) -> None:
        depth = self._expected_depth(depth)
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            return
        polygon = polygon.buffer(self.xy_spacing * 0.5)
        min_x, min_y, max_x, max_y = polygon.bounds
        yy, xx = self._window_mesh(min_x, min_y, max_x, max_y)
        if xx.size == 0:
            return
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        mask = contains_xy(polygon, xx, yy)
        target = self.expected_depth[y_slice, x_slice]
        np.maximum(target, depth, out=target, where=mask)

    def expected_swept_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        start_depth: float,
        end_depth: float,
        radius: float,
    ) -> None:
        min_x = min(start[0], end[0]) - radius
        max_x = max(start[0], end[0]) + radius
        min_y = min(start[1], end[1]) - radius
        max_y = max(start[1], end[1]) + radius
        yy, xx = self._window_mesh(min_x, min_y, max_x, max_y)
        if xx.size == 0:
            return
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            radial_sq = (xx - start[0]) ** 2 + (yy - start[1]) ** 2
            mask = radial_sq <= radius**2
            depth = np.full_like(xx, max(start_depth, end_depth), dtype=np.float32)
        else:
            t = ((xx - start[0]) * dx + (yy - start[1]) * dy) / length_sq
            t = np.clip(t, 0.0, 1.0)
            nearest_x = start[0] + t * dx
            nearest_y = start[1] + t * dy
            radial_sq = (xx - nearest_x) ** 2 + (yy - nearest_y) ** 2
            mask = radial_sq <= radius**2
            depth = (start_depth + t * (end_depth - start_depth)).astype(np.float32)
        target = self.expected_depth[y_slice, x_slice]
        np.maximum(target, np.minimum(depth, self.stock.thickness), out=target, where=mask)

    def _expected_depth(self, depth: float) -> float:
        return min(float(depth), float(self.stock.thickness))

    def remove_circle(
        self,
        center: tuple[float, float],
        radius: float,
        depth: float,
        operation_id: str,
        profile: ToolProfile | None = None,
    ) -> int:
        yy, xx = self._window_mesh(center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
        if xx.size == 0:
            return 0
        x_slice, y_slice = self._window_slices(center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
        radial_sq = (xx - center[0]) ** 2 + (yy - center[1]) ** 2
        mask = radial_sq <= radius**2
        cut_depth = _profile_depth(depth, np.sqrt(np.maximum(radial_sq, 0.0)), profile)
        return self._apply_removal(x_slice, y_slice, mask, cut_depth, operation_id)

    def remove_swept_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        start_depth: float,
        end_depth: float,
        radius: float,
        operation_id: str,
        profile: ToolProfile | None = None,
    ) -> int:
        min_x = min(start[0], end[0]) - radius
        max_x = max(start[0], end[0]) + radius
        min_y = min(start[1], end[1]) - radius
        max_y = max(start[1], end[1]) + radius
        yy, xx = self._window_mesh(min_x, min_y, max_x, max_y)
        if xx.size == 0:
            return 0
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return self.remove_circle(start, radius, max(start_depth, end_depth), operation_id, profile)
        t = ((xx - start[0]) * dx + (yy - start[1]) * dy) / length_sq
        t = np.clip(t, 0.0, 1.0)
        nearest_x = start[0] + t * dx
        nearest_y = start[1] + t * dy
        radial_sq = (xx - nearest_x) ** 2 + (yy - nearest_y) ** 2
        mask = radial_sq <= radius**2
        depth = start_depth + t * (end_depth - start_depth)
        depth = _profile_depth(depth, np.sqrt(np.maximum(radial_sq, 0.0)), profile)
        return self._apply_removal(x_slice, y_slice, mask, depth.astype(np.float32), operation_id)

    def would_remove_swept_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        start_depth: float,
        end_depth: float,
        radius: float,
    ) -> bool:
        min_x = min(start[0], end[0]) - radius
        max_x = max(start[0], end[0]) + radius
        min_y = min(start[1], end[1]) - radius
        max_y = max(start[1], end[1]) + radius
        yy, xx = self._window_mesh(min_x, min_y, max_x, max_y)
        if xx.size == 0:
            return False
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            mask = (xx - start[0]) ** 2 + (yy - start[1]) ** 2 <= radius**2
            depth = max(start_depth, end_depth)
        else:
            t = ((xx - start[0]) * dx + (yy - start[1]) * dy) / length_sq
            t = np.clip(t, 0.0, 1.0)
            nearest_x = start[0] + t * dx
            nearest_y = start[1] + t * dy
            mask = (xx - nearest_x) ** 2 + (yy - nearest_y) ** 2 <= radius**2
            depth = start_depth + t * (end_depth - start_depth)
        current = self.actual_depth[y_slice, x_slice]
        return bool(np.any(mask & (depth > current + 1e-6)))

    def _apply_removal(
        self,
        x_slice: slice,
        y_slice: slice,
        mask: np.ndarray,
        depth: np.ndarray,
        operation_id: str,
    ) -> int:
        depth = np.minimum(depth, self.stock.thickness).astype(np.float32)
        current = self.actual_depth[y_slice, x_slice]
        changed = mask & (depth > current + 1e-6)
        touched = mask & (depth > 1e-9)
        np.maximum(current, depth, out=current, where=mask)
        if np.any(touched):
            cut_window = self.cut_count[y_slice, x_slice]
            cut_window[touched] = np.minimum(cut_window[touched].astype(np.uint32) + 1, np.iinfo(np.uint16).max)
            self.last_operation_id[y_slice, x_slice][touched] = self.operation_index(operation_id)
        return int(np.count_nonzero(changed))

    def _window_slices(self, min_x: float, min_y: float, max_x: float, max_y: float) -> tuple[slice, slice]:
        x0 = int(np.searchsorted(self.x_values, min_x, side="left"))
        x1 = int(np.searchsorted(self.x_values, max_x, side="right"))
        y0 = int(np.searchsorted(self.y_values, min_y, side="left"))
        y1 = int(np.searchsorted(self.y_values, max_y, side="right"))
        return slice(max(0, x0), min(len(self.x_values), x1)), slice(max(0, y0), min(len(self.y_values), y1))

    def _window_mesh(self, min_x: float, min_y: float, max_x: float, max_y: float) -> tuple[np.ndarray, np.ndarray]:
        x_slice, y_slice = self._window_slices(min_x, min_y, max_x, max_y)
        if _empty_slice(x_slice) or _empty_slice(y_slice):
            empty = np.array([], dtype=np.float32)
            return empty, empty
        return np.meshgrid(self.y_values[y_slice], self.x_values[x_slice], indexing="ij")

    def snapshot(self) -> SimulationSnapshot:
        return SimulationSnapshot(
            x_values=[float(value) for value in self.x_values],
            y_values=[float(value) for value in self.y_values],
            actual_depth=self.actual_depth.astype(float).tolist(),
            expected_depth=self.expected_depth.astype(float).tolist(),
            cut_count=self.cut_count.astype(int).tolist(),
            last_operation_id=self.last_operation_id.astype(int).tolist(),
            operation_lookup={int(key): value for key, value in self.operation_lookup.items()},
        )


def _axis_centers(min_value: float, max_value: float, spacing: float) -> np.ndarray:
    count = max(1, int(np.ceil((max_value - min_value) / spacing)))
    return min_value + (np.arange(count, dtype=np.float32) + 0.5) * spacing


def _empty_slice(value: slice) -> bool:
    return value.stop is None or value.start is None or value.stop <= value.start


def _profile_depth(center_depth, radial_distance: np.ndarray, profile: ToolProfile | None):
    if profile is None or profile.end_type in {"flat", "o-flute"}:
        return np.asarray(center_depth, dtype=np.float32)
    radius = profile.diameter / 2
    inside = np.maximum(radius * radius - radial_distance * radial_distance, 0.0)
    ball_lift = radius - np.sqrt(inside)
    return np.maximum(np.asarray(center_depth, dtype=np.float32) - ball_lift, 0.0)
