from __future__ import annotations

from io import BytesIO

import numpy as np
from matplotlib.colors import ListedColormap, BoundaryNorm

from dxfwiz.simulation.dexel import DexelGrid


def render_dexel_preview_png(
    grid: DexelGrid,
    title: str,
    depth: np.ndarray | None = None,
    max_surface_points: int = 160,
    state_view: bool = True,
    state_mode: str = "binary",
    overlay_expected: bool = False,
) -> bytes:
    """Render top and isometric views to PNG bytes.

    The simulation service can return arrays/metrics without knowing anything about files;
    tests or UI callers can persist these bytes wherever they want.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    depth_values = np.asarray(depth if depth is not None else grid.actual_depth, dtype=float)
    expected_values = np.asarray(grid.expected_depth, dtype=float)
    if state_view:
        state_values = _final_state_values(
            depth_values,
            expected_values,
            grid.stock.thickness,
            grid.xy_spacing,
            state_mode,
        )
        display_values = state_values
        if state_mode == "binary":
            cmap = ListedColormap(["#d8caa8", "#16a34a"])
            norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
        else:
            cmap = ListedColormap(["#d8caa8", "#16a34a", "#f97316", "#ef4444", "#64748b"])
            norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5, 4.5], cmap.N)
    else:
        display_values = depth_values
        cmap = "viridis"
        norm = None
    x_grid, y_grid = np.meshgrid(grid.x_values, grid.y_values)
    stride = max(1, int(max(depth_values.shape) / max_surface_points))
    surface_z = grid.stock.thickness - depth_values

    fig = plt.figure(figsize=(12, 5), dpi=140)
    fig.suptitle(title, fontsize=13, fontweight="bold")

    top_ax = fig.add_subplot(1, 2, 1)
    image = top_ax.imshow(
        display_values,
        extent=(grid.bounds.min_x, grid.bounds.max_x, grid.bounds.min_y, grid.bounds.max_y),
        origin="lower",
        cmap=cmap,
        norm=norm,
        vmin=None if state_view else 0,
        vmax=None if state_view else max(grid.stock.thickness, float(depth_values.max(initial=0.0))),
        interpolation="nearest",
    )
    top_ax.set_title("Top view: final material state" if state_view else "Top view: removed depth")
    top_ax.set_aspect("equal", adjustable="box")
    top_ax.set_xlabel("X")
    top_ax.set_ylabel("Y")
    if overlay_expected and np.any(grid.expected_depth > 1e-9):
        top_ax.contour(
            x_grid,
            y_grid,
            grid.expected_depth,
            levels=[float(grid.expected_depth.max()) * 0.5],
            colors=["#f97316"],
            linewidths=1.2,
        )
    colorbar = fig.colorbar(image, ax=top_ax, fraction=0.046, pad=0.04)
    if state_view:
        if state_mode == "binary":
            colorbar.set_ticks([0, 1])
            colorbar.set_ticklabels(["stock", "cut"])
        else:
            colorbar.set_ticks([0, 1, 2, 3, 4])
            colorbar.set_ticklabels(["stock", "removed", "partial", "overcut", "cut"])

    iso_ax = fig.add_subplot(1, 2, 2, projection="3d")
    sampled_x = x_grid[::stride, ::stride]
    sampled_y = y_grid[::stride, ::stride]
    sampled_z = surface_z[::stride, ::stride]
    sampled_depth = depth_values[::stride, ::stride]
    iso_ax.plot_surface(
        sampled_x,
        sampled_y,
        sampled_z,
        facecolors=_surface_colors(plt, sampled_depth, expected_values[::stride, ::stride], grid, state_view, state_mode),
        linewidth=0,
        antialiased=False,
        shade=False,
        alpha=0.95,
    )
    iso_ax.set_title("Isometric stock view")
    iso_ax.set_xlabel("X")
    iso_ax.set_ylabel("Y")
    iso_ax.set_zlabel("remaining Z")
    iso_ax.view_init(elev=30, azim=-45)
    _set_equal_3d(iso_ax, grid, sampled_z)

    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def _normalized(values: np.ndarray, maximum: float) -> np.ndarray:
    if maximum <= 0:
        return np.zeros_like(values, dtype=float)
    return np.clip(values / maximum, 0.0, 1.0)


def _final_state_values(
    actual_depth: np.ndarray,
    expected_depth: np.ndarray,
    stock_thickness: float,
    xy_spacing: float,
    state_mode: str = "binary",
) -> np.ndarray:
    tolerance = max(xy_spacing * 0.1, 1e-6)
    if state_mode == "binary":
        return (actual_depth > tolerance).astype(np.uint8)
    state = np.zeros(actual_depth.shape, dtype=np.uint8)
    expected_cut = expected_depth > tolerance
    actual_cut = actual_depth > tolerance
    state[actual_cut & ~expected_cut] = 3
    state[actual_cut & expected_cut & (actual_depth < expected_depth - tolerance)] = 2
    state[expected_cut & (actual_depth >= expected_depth - tolerance)] = 1
    state[actual_cut & ~expected_cut & (actual_depth >= stock_thickness - tolerance)] = 4
    return state


def _surface_colors(plt, depth: np.ndarray, expected: np.ndarray, grid: DexelGrid, state_view: bool, state_mode: str):
    if not state_view:
        return plt.cm.viridis(_normalized(depth, grid.stock.thickness))
    state = _final_state_values(depth, expected, grid.stock.thickness, grid.xy_spacing, state_mode)
    if state_mode == "binary":
        palette = np.array(
            [
                [0.8471, 0.7922, 0.6588, 1.0],
                [0.0863, 0.6392, 0.2902, 1.0],
            ]
        )
    else:
        palette = np.array(
            [
                [0.8471, 0.7922, 0.6588, 1.0],
                [0.0863, 0.6392, 0.2902, 1.0],
                [0.9765, 0.4510, 0.0863, 1.0],
                [0.9373, 0.2667, 0.2667, 1.0],
                [0.3922, 0.4549, 0.5451, 1.0],
            ]
        )
    return palette[state]


def _set_equal_3d(ax, grid: DexelGrid, z_values: np.ndarray) -> None:
    x_min, x_max = grid.bounds.min_x, grid.bounds.max_x
    y_min, y_max = grid.bounds.min_y, grid.bounds.max_y
    z_min = min(0.0, float(z_values.min(initial=0.0)))
    z_max = grid.stock.thickness
    max_range = max(x_max - x_min, y_max - y_min, z_max - z_min)
    x_mid = (x_min + x_max) / 2
    y_mid = (y_min + y_max) / 2
    z_mid = (z_min + z_max) / 2
    ax.set_xlim(x_mid - max_range / 2, x_mid + max_range / 2)
    ax.set_ylim(y_mid - max_range / 2, y_mid + max_range / 2)
    ax.set_zlim(z_mid - max_range / 2, z_mid + max_range / 2)
