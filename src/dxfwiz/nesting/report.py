from __future__ import annotations

from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import ezdxf
from shapely.geometry.base import BaseGeometry

from dxfwiz.dxf import clean_dxf
from dxfwiz.nesting.engine import NestingResult, _entity_polygon


def write_nesting_png(
    *,
    source_dxf: str | Path | Iterable[str | Path],
    result: NestingResult,
    output_path: str | Path,
    title: str,
) -> Path:
    source_dxfs = _source_paths(source_dxf)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(1, 2, figsize=(14, 7), constrained_layout=True)
    fig.suptitle(title)

    _draw_sources(axes[0], source_dxfs)
    axes[0].set_title("source human packing")
    axes[0].set_aspect("equal", adjustable="box")

    axes[1].set_title(
        f"generated nest: {len(result.placements)} placed, "
        f"{len(result.unplaced)} unplaced, "
        f"{result.used_width:.2f} x {result.used_height:.2f}, "
        f"{result.spacing:.3f} clearance"
    )
    axes[1].add_patch(
        plt.Rectangle(
            (0, 0),
            result.stock_width,
            max(result.stock_height, result.used_height),
            fill=False,
            edgecolor="#222222",
            linewidth=1.5,
        )
    )
    for index, placement in enumerate(result.placements):
        _draw_geometry(axes[1], placement.footprint, "#94a3b8", alpha=0.18)
        _draw_geometry(axes[1], placement.geometry, _color(index), alpha=0.55)
        cx = (placement.geometry.bounds[0] + placement.geometry.bounds[2]) / 2
        cy = (placement.geometry.bounds[1] + placement.geometry.bounds[3]) / 2
        axes[1].text(cx, cy, str(placement.part_index + 1), ha="center", va="center", fontsize=8)
    axes[1].set_xlim(-0.5, result.stock_width + 0.5)
    axes[1].set_ylim(-0.5, max(result.stock_height, result.used_height) + 0.5)
    axes[1].set_aspect("equal", adjustable="box")

    for axis in axes:
        axis.grid(True, color="#e2e8f0", linewidth=0.6)
        axis.set_xlabel("x")
        axis.set_ylabel("y")

    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def write_nesting_index(output_root: str | Path, cases: list[dict]) -> Path:
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        image = escape(Path(case["image"]).as_posix())
        rows.append(
            f"""
            <section class="case">
              <div class="case-head">
                <h2>{escape(case["name"])}</h2>
                <span>{case["placed"]} placed</span>
                <span>{case["unplaced"]} unplaced</span>
                <span>{case["used_width"]:.2f} x {case["used_height"]:.2f}</span>
              </div>
              <a href="{image}"><img src="{image}" alt="{escape(case["name"])} nesting output"></a>
            </section>
            """
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Nesting Test Outputs</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      color: #172033;
      background: #f7f9fb;
    }}
    header {{
      padding: 24px 32px;
      background: #ffffff;
      border-bottom: 1px solid #d8dee8;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    h1, h2 {{
      margin: 0;
    }}
    main {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(520px, 1fr));
      gap: 20px;
      padding: 20px;
    }}
    .case {{
      background: #ffffff;
      border: 1px solid #d8dee8;
      border-radius: 8px;
      overflow: hidden;
    }}
    .case-head {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid #e5eaf1;
    }}
    .case-head h2 {{
      margin-right: auto;
      font-size: 18px;
    }}
    .case-head span {{
      font-size: 13px;
      background: #eef3f8;
      border-radius: 999px;
      padding: 5px 9px;
    }}
    img {{
      display: block;
      width: 100%;
      height: auto;
    }}
  </style>
</head>
<body>
  <header><h1>Nesting Test Outputs</h1></header>
  <main>
    {"".join(rows)}
  </main>
</body>
</html>
"""
    index_path = output_root / "index.html"
    index_path.write_text(html, encoding="utf-8")
    return index_path


def _source_paths(source_dxf: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(source_dxf, str | Path):
        return [Path(source_dxf)]
    return [Path(path) for path in source_dxf]


def _draw_sources(axis, source_dxfs: list[Path]) -> None:
    offset_x = 0.0
    all_bounds = []
    for source_dxf in source_dxfs:
        bounds = _draw_source(axis, source_dxf, offset_x)
        if bounds is None:
            continue
        all_bounds.append(bounds)
        offset_x = bounds[2] + max(bounds[2] - bounds[0], 1.0) * 0.1
    if all_bounds:
        min_x = min(bound[0] for bound in all_bounds)
        min_y = min(bound[1] for bound in all_bounds)
        max_x = max(bound[2] for bound in all_bounds)
        max_y = max(bound[3] for bound in all_bounds)
        axis.set_xlim(min_x - 0.5, max_x + 0.5)
        axis.set_ylim(min_y - 0.5, max_y + 0.5)


def _draw_source(axis, source_dxf: Path, offset_x: float) -> tuple[float, float, float, float] | None:
    with TemporaryDirectory(prefix="dxfwiz_nesting_report_") as temp_dir_name:
        fixed_path = Path(temp_dir_name) / f"{source_dxf.stem}_fixed.dxf"
        clean_dxf(source_dxf, fixed_path)
        doc = ezdxf.readfile(fixed_path)
        polygons = [_entity_polygon(entity) for entity in doc.modelspace()]
    polygons = [polygon for polygon in polygons if polygon is not None]
    if not polygons:
        return None
    min_x = min(polygon.bounds[0] for polygon in polygons)
    min_y = min(polygon.bounds[1] for polygon in polygons)
    max_x = max(polygon.bounds[2] for polygon in polygons)
    max_y = max(polygon.bounds[3] for polygon in polygons)
    for polygon in polygons:
        from shapely import affinity

        shifted = affinity.translate(polygon, xoff=offset_x - min_x)
        _draw_geometry(axis, shifted, "#6b7280", alpha=0.25)
    return offset_x, min_y, offset_x + (max_x - min_x), max_y


def _draw_geometry(axis, geometry: BaseGeometry, color: str, alpha: float) -> None:
    geometries = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    for polygon in geometries:
        if polygon.is_empty:
            continue
        x, y = polygon.exterior.xy
        axis.fill(x, y, facecolor=color, edgecolor="#111827", linewidth=0.7, alpha=alpha)
        for interior in polygon.interiors:
            hx, hy = interior.xy
            axis.fill(hx, hy, facecolor="white", edgecolor="#111827", linewidth=0.5, alpha=1.0)


def _color(index: int) -> str:
    colors = [
        "#14b8a6",
        "#f97316",
        "#6366f1",
        "#84cc16",
        "#ec4899",
        "#06b6d4",
        "#f59e0b",
        "#8b5cf6",
        "#22c55e",
    ]
    return colors[index % len(colors)]
