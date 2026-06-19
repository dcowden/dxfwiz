from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from html import escape
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry

from dxfwiz.schemas import GeometryFile, MachineFile
from dxfwiz.schemas.job import (
    ContourOperation,
    DrillOperation,
    HelicalContourOperation,
    HelicalPocketOperation,
    JobFile,
    MoveOperation,
    PocketOperation,
    TraceOperation,
)
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove, SourcePath, ToolpathPass, ToolpathPlan
from dxfwiz.toolpaths.operations import source_path_points


CAMOTICS_RESOLUTION_MM = 0.254
EDGE_TOLERANCE_IN = max(0.015, CAMOTICS_RESOLUTION_MM * 3 / 25.4)
Z_TOLERANCE_IN = max(0.004, CAMOTICS_RESOLUTION_MM * 1.5 / 25.4)


@dataclass(frozen=True)
class ExpectedSurfaceRegion:
    geometry: BaseGeometry
    depth_z: float


@dataclass(frozen=True)
class CamoticsMesh:
    triangles: int
    normal_z: np.ndarray
    center_xy_in: np.ndarray
    center_z_in: np.ndarray
    z_span_mm: np.ndarray
    max_z_mm: float


@dataclass(frozen=True)
class CamoticsAnalysis:
    triangles: int
    horizontal_facets: int
    checked_facets: int
    boundary_skipped_facets: int
    z_violating_facets: int
    material_violating_facets: int
    wall_facets: int
    wall_checked_facets: int
    wall_violating_facets: int
    expected_region_count: int
    edge_tolerance_in: float
    z_tolerance_in: float
    exterior_stock_points: tuple[tuple[float, float], ...]
    floor_points: tuple[tuple[float, float], ...]
    violation_points: tuple[tuple[float, float, float, float], ...]
    material_violation_points: tuple[tuple[float, float, float, float], ...]
    wall_points: tuple[tuple[float, float], ...]
    wall_violation_points: tuple[tuple[float, float], ...]
    triangle_colors: str


@dataclass(frozen=True)
class CamoticsArtifacts:
    output_dir: Path
    nc_path: Path
    project_path: Path
    stl_path: Path
    png_path: Path
    html_path: Path


def run_camotics_material_validation(
    *,
    name: str,
    gcode: str,
    job: JobFile,
    geometry: GeometryFile,
    machine: MachineFile,
    toolpath_plan: ToolpathPlan,
    output_dir: Path,
    stock_bounds: tuple[float, float, float, float] | None = None,
    operation_ids: set[str] | None = None,
    timeout_seconds: int = 60,
) -> tuple[CamoticsArtifacts, CamoticsAnalysis]:
    camsim = find_camsim()
    if camsim is None:
        raise RuntimeError("CAMotics camsim executable was not found")

    artifacts = CamoticsArtifacts(
        output_dir=output_dir,
        nc_path=output_dir / "generated.nc",
        project_path=output_dir / "project.camotics",
        stl_path=output_dir / "simulated.stl",
        png_path=output_dir / "simulated.png",
        html_path=output_dir / "report.html",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_regions = expected_surface_regions(job, geometry, machine, toolpath_plan, operation_ids=operation_ids)
    stock_bounds = stock_bounds or _expected_region_bounds(expected_regions)
    artifacts.nc_path.write_text(camotics_gcode(gcode), encoding="utf-8")
    artifacts.project_path.write_text(
        json.dumps(
            camotics_project(
                machine=machine,
                job=job,
                gcode_name=artifacts.nc_path.name,
            stock_bounds=stock_bounds,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
            [
                str(camsim),
                "--binary",
                "--resolution",
                str(CAMOTICS_RESOLUTION_MM),
            "--threads",
            str(_camotics_thread_count()),
            artifacts.project_path.name,
            artifacts.stl_path.name,
        ],
        cwd=artifacts.output_dir,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    mesh = load_camotics_stl(artifacts.stl_path)
    analysis = analyze_camotics_stl(mesh, expected_regions)
    render_camotics_png(name, artifacts.png_path, stock_bounds, expected_regions, analysis)
    render_camotics_html(name, artifacts, analysis)
    return artifacts, analysis


def _camotics_thread_count() -> int:
    value = os.environ.get("DXFWIZ_CAMOTICS_THREADS")
    if value is None:
        return 6
    try:
        return max(1, int(value))
    except ValueError:
        return 4


def find_camsim() -> Path | None:
    found = shutil.which("camsim")
    if found:
        return Path(found)
    for candidate in (
        Path(r"C:\Program Files\CAMotics\camsim.exe"),
        Path(r"C:\Program Files (x86)\CAMotics\camsim.exe"),
    ):
        if candidate.exists():
            return candidate
    return None


def camotics_gcode(gcode: str) -> str:
    lines = []
    for line in gcode.splitlines():
        line = re.sub(r"\([^)]*\)", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


def camotics_project(
    *,
    machine: MachineFile,
    job: JobFile,
    gcode_name: str,
    stock_bounds: tuple[float, float, float, float],
) -> dict:
    min_x, min_y, max_x, max_y = stock_bounds
    max_tool_diameter = max((tool.diameter for tool in machine.tools), default=0.25)
    margin = max(max_tool_diameter, 0.1)
    stock_bottom = -(job.stock.thickness + margin)
    scale = 25.4 if job.units.length == "in" else 1.0
    tool_units = "imperial" if job.units.length == "in" else "metric"
    tools = {}
    for tool in machine.tools:
        tool_number = _tool_number(tool.id)
        tools[str(tool_number)] = {
            "units": tool_units,
            "shape": "cylindrical",
            "length": tool.total_length or max(tool.flute_length or 0.0, job.stock.thickness + margin, 1.0),
            "diameter": tool.diameter,
            "description": tool.description,
        }
    return {
        "units": "metric",
        "resolution-mode": "high",
        "resolution": CAMOTICS_RESOLUTION_MM,
        "tools": tools or {
            "1": {
                "units": tool_units,
                "shape": "cylindrical",
                "length": 1.0,
                "diameter": max_tool_diameter,
                "description": "fallback flat end mill",
            }
        },
        "workpiece": {
            "automatic": False,
            "margin": 0,
            "bounds": {
                "min": [(min_x - margin) * scale, (min_y - margin) * scale, stock_bottom * scale],
                "max": [(max_x + margin) * scale, (max_y + margin) * scale, 0],
            },
        },
        "files": [gcode_name],
    }


def expected_surface_regions(
    job: JobFile,
    geometry: GeometryFile,
    machine: MachineFile,
    toolpath_plan: ToolpathPlan,
    *,
    operation_ids: set[str] | None = None,
) -> list[ExpectedSurfaceRegion]:
    entities = {entity.id: entity for entity in geometry.entities}
    generated = {entity.id: entity for entity in job.generated_entities}
    generated.update({entity.id: entity for entity in geometry.generated_entities})
    source_paths = {source_path.entity: source_path for source_path in toolpath_plan.source_paths}
    tools = {tool.id: tool for tool in machine.tools}

    regions: list[ExpectedSurfaceRegion] = []
    for operation in job.operations:
        if operation_ids is not None and operation.id not in operation_ids:
            continue
        tool = tools.get(getattr(operation, "tool", ""))
        tool_radius = (tool.diameter / 2) if tool is not None else 0.0
        if isinstance(operation, PocketOperation):
            geometry_for_op = _entity_geometry(operation.entity, entities, generated, source_paths)
            if geometry_for_op is not None and not geometry_for_op.is_empty:
                floor = _cutter_accessible_floor(geometry_for_op, tool_radius)
                if not floor.is_empty:
                    regions.append(ExpectedSurfaceRegion(floor, -operation.depth))
        elif isinstance(operation, DrillOperation | HelicalContourOperation | HelicalPocketOperation):
            circle = _circle_geometry(job, operation, entities, generated, tool_radius)
            if circle is not None and not circle.is_empty:
                depth = _drill_expected_depth(job, operation, generated.get(operation.entity))
                regions.append(ExpectedSurfaceRegion(circle, -depth))
        elif isinstance(operation, ContourOperation | TraceOperation):
            regions.extend(_swept_regions_for_operation(toolpath_plan, operation.id))
        elif isinstance(operation, MoveOperation):
            continue
    return regions


def _expected_region_bounds(regions: list[ExpectedSurfaceRegion]) -> tuple[float, float, float, float]:
    if not regions:
        return (-0.5, -0.5, 0.5, 0.5)
    union = shapely.union_all([region.geometry for region in regions])
    min_x, min_y, max_x, max_y = union.bounds
    if max_x - min_x < 1e-6:
        min_x -= 0.5
        max_x += 0.5
    if max_y - min_y < 1e-6:
        min_y -= 0.5
        max_y += 0.5
    return float(min_x), float(min_y), float(max_x), float(max_y)


def _entity_geometry(entity_id: str, entities: dict, generated: dict, source_paths: dict[str, SourcePath]) -> BaseGeometry | None:
    source_path = source_paths.get(entity_id)
    if source_path is not None and source_path.closed:
        points = source_path_points(source_path, arc_segments=96)
        if len(points) >= 3:
            polygon = Polygon(points)
            return polygon.buffer(0) if not polygon.is_valid else polygon

    entity = entities.get(entity_id) or generated.get(entity_id)
    if entity is None:
        return None
    center = getattr(entity, "center", None)
    diameter = getattr(entity, "diameter", None)
    if center is not None and diameter is not None:
        return Point(center.x, center.y).buffer(diameter / 2, resolution=64)
    lower_left = getattr(entity, "lower_left", None)
    upper_right = getattr(entity, "upper_right", None)
    if lower_left is not None and upper_right is not None:
        return box(lower_left.x, lower_left.y, upper_right.x, upper_right.y)
    bbox = getattr(entity, "bounding_box", None)
    if bbox is not None:
        return box(bbox.min.x, bbox.min.y, bbox.max.x, bbox.max.y)
    return None


def _cutter_accessible_floor(geometry: BaseGeometry, cutter_radius: float) -> BaseGeometry:
    if cutter_radius <= 1e-9:
        return geometry
    opened = geometry.buffer(-cutter_radius, join_style=1)
    if opened.is_empty:
        return opened
    return opened.buffer(cutter_radius, join_style=1).intersection(geometry)


def _circle_geometry(job: JobFile, operation, entities: dict, generated: dict, tool_radius: float) -> BaseGeometry | None:
    entity = entities.get(operation.entity) or generated.get(operation.entity)
    center = getattr(entity, "center", None) if entity is not None else None
    diameter = getattr(operation, "hole_diameter", None) or getattr(entity, "diameter", None)
    if center is None or diameter is None:
        return None
    radius = max(diameter / 2, tool_radius)
    return Point(center.x, center.y).buffer(radius, resolution=64)


def _drill_expected_depth(job: JobFile, operation, entity) -> float:
    if isinstance(operation, DrillOperation) and getattr(entity, "role", None) == "screw_hole":
        return max(operation.depth, job.stock.thickness)
    return operation.depth


def _swept_regions_for_operation(toolpath_plan: ToolpathPlan, operation_id: str) -> list[ExpectedSurfaceRegion]:
    regions: list[ExpectedSurfaceRegion] = []
    position = (0.0, 0.0, 0.0)
    for toolpath_pass in toolpath_plan.passes:
        if toolpath_pass.operation_id != operation_id:
            continue
        radius = (toolpath_pass.tool_diameter or 0.0) / 2
        if radius <= 0:
            continue
        position = _append_swept_pass_regions(toolpath_pass, position, radius, regions)
    return regions


def _append_swept_pass_regions(
    toolpath_pass: ToolpathPass,
    position: tuple[float, float, float],
    radius: float,
    regions: list[ExpectedSurfaceRegion],
) -> tuple[float, float, float]:
    for move in toolpath_pass.moves:
        if isinstance(move, RapidMove):
            position = _next_position(position, move)
        elif isinstance(move, LineMove):
            next_position = _next_position(position, move)
            _append_swept_region(position, next_position, radius, regions)
            position = next_position
        elif isinstance(move, ArcMove):
            points = _arc_points(position, move)
            start = position
            for end in points:
                _append_swept_region(start, end, radius, regions)
                start = end
            position = points[-1] if points else _next_position(position, move)
    return position


def _append_swept_region(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    regions: list[ExpectedSurfaceRegion],
) -> None:
    depth = max(_depth_from_z(start[2]), _depth_from_z(end[2]))
    if depth <= 1e-9:
        return
    line = LineString([(start[0], start[1]), (end[0], end[1])])
    if line.length <= 1e-9:
        return
    regions.append(ExpectedSurfaceRegion(line.buffer(radius, cap_style=1, join_style=1), -depth))


def load_camotics_stl(stl_path: Path) -> CamoticsMesh:
    file_size = stl_path.stat().st_size
    if file_size < 84:
        raise AssertionError(f"{stl_path} is too small to be a binary STL")
    with stl_path.open("rb") as handle:
        handle.seek(80)
        triangles = struct.unpack("<I", handle.read(4))[0]
    expected_size = 84 + triangles * 50
    if file_size < expected_size:
        raise AssertionError(f"{stl_path} is truncated")
    dtype = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    records = np.memmap(stl_path, dtype=dtype, mode="r", offset=84, shape=(triangles,))
    vertices = records["vertices"]
    centers = vertices.mean(axis=1)
    z_values = vertices[:, :, 2]
    return CamoticsMesh(
        triangles=triangles,
        normal_z=np.asarray(records["normal"][:, 2], dtype=np.float32),
        center_xy_in=np.asarray(centers[:, :2] / 25.4, dtype=np.float32),
        center_z_in=np.asarray(centers[:, 2] / 25.4, dtype=np.float32),
        z_span_mm=np.asarray(np.ptp(z_values, axis=1), dtype=np.float32),
        max_z_mm=float(z_values.max()),
    )


def analyze_camotics_stl(mesh: CamoticsMesh, expected_regions: list[ExpectedSurfaceRegion]) -> CamoticsAnalysis:
    horizontal_z_tolerance_mm = max(0.20, CAMOTICS_RESOLUTION_MM * 1.1)
    center_xy_in = mesh.center_xy_in
    center_z_in = mesh.center_z_in
    normal_z = mesh.normal_z
    expected_z = np.zeros(mesh.triangles, dtype=np.float32)
    boundary_distance = np.full(mesh.triangles, np.inf, dtype=np.float32)

    min_depth = min((region.depth_z for region in expected_regions), default=0.0)
    horizontal_candidates = (np.abs(normal_z) >= 0.75) & (mesh.z_span_mm <= horizontal_z_tolerance_mm)
    horizontal_ids = _sorted_z_range_ids(
        horizontal_candidates,
        center_z_in,
        min_depth - Z_TOLERANCE_IN,
        Z_TOLERANCE_IN,
    )
    vertical_candidates = (
        (np.abs(normal_z) <= 0.25)
        & (center_z_in <= Z_TOLERANCE_IN)
        & (center_z_in >= min_depth - Z_TOLERANCE_IN)
    )
    vertical_ids = _sorted_z_range_ids(
        vertical_candidates,
        center_z_in,
        min_depth - Z_TOLERANCE_IN,
        Z_TOLERANCE_IN,
    )
    query_ids = np.union1d(horizontal_ids, vertical_ids)
    if len(query_ids):
        points = shapely.points(center_xy_in[query_ids, 0], center_xy_in[query_ids, 1])
        expected_z[query_ids] = _expected_surface_z_many(points, expected_regions).astype(np.float32)
        boundary_distance[query_ids] = _boundary_distance_many_numpy(
            center_xy_in[query_ids],
            expected_regions,
            max(EDGE_TOLERANCE_IN * 3, 0.06),
        )

    horizontal_mask = np.zeros(mesh.triangles, dtype=bool)
    horizontal_mask[horizontal_ids] = True
    stock_underside_mask = (expected_z < -1e-9) & (normal_z < -0.75) & (center_z_in < expected_z - Z_TOLERANCE_IN)
    horizontal_mask &= ~stock_underside_mask
    horizontal_mask &= ~((expected_z >= -1e-9) & (normal_z < 0.75))
    edge_mask = horizontal_mask & (boundary_distance <= EDGE_TOLERANCE_IN)
    check_mask = horizontal_mask & ~edge_mask
    floor_mask = check_mask & (expected_z < -1e-9)
    exterior_stock_mask = check_mask & (expected_z >= -1e-9)
    z_bad_mask = check_mask & (np.abs(center_z_in - expected_z) > Z_TOLERANCE_IN)
    material_bad_mask = (
        (expected_z < -1e-9)
        & (center_z_in > expected_z + Z_TOLERANCE_IN)
        & (center_z_in <= Z_TOLERANCE_IN)
        & (boundary_distance > EDGE_TOLERANCE_IN)
    )

    wall_search_tolerance = max(EDGE_TOLERANCE_IN * 3, 0.06)
    vertical_candidate_mask = np.zeros(mesh.triangles, dtype=bool)
    vertical_candidate_mask[vertical_ids] = True
    relevant_wall_mask = (expected_z < -1e-9) | (boundary_distance <= wall_search_tolerance)
    vertical_mask = vertical_candidate_mask & relevant_wall_mask
    wall_ok_mask = vertical_mask & (boundary_distance <= EDGE_TOLERANCE_IN)
    wall_bad_mask = vertical_mask & ~wall_ok_mask
    if not _strict_wall_validation_enabled():
        wall_ok_mask = vertical_mask
        wall_bad_mask = np.zeros(len(vertical_mask), dtype=bool)
    bad_visual_mask = _expand_xy_mask_numpy(
        center_xy_in,
        z_bad_mask | material_bad_mask | wall_bad_mask,
        EDGE_TOLERANCE_IN,
    )
    triangle_colors = _triangle_colors(
        len(center_xy_in),
        exterior_stock_mask=exterior_stock_mask,
        floor_mask=floor_mask,
        edge_mask=edge_mask,
        wall_ok_mask=wall_ok_mask,
        bad_visual_mask=bad_visual_mask,
    )

    return CamoticsAnalysis(
        triangles=mesh.triangles,
        horizontal_facets=int(horizontal_mask.sum()),
        checked_facets=int(check_mask.sum()),
        boundary_skipped_facets=int(edge_mask.sum()),
        z_violating_facets=int(z_bad_mask.sum()),
        material_violating_facets=int(material_bad_mask.sum()),
        wall_facets=int(vertical_mask.sum()),
        wall_checked_facets=int(wall_ok_mask.sum()),
        wall_violating_facets=int(wall_bad_mask.sum()),
        expected_region_count=len(expected_regions),
        edge_tolerance_in=EDGE_TOLERANCE_IN,
        z_tolerance_in=Z_TOLERANCE_IN,
        exterior_stock_points=tuple(_sample_xy_array(center_xy_in[exterior_stock_mask], 6000)),
        floor_points=tuple(_sample_xy_array(center_xy_in[floor_mask], 6000)),
        violation_points=tuple(_sample_violation_array(center_xy_in[z_bad_mask], center_z_in[z_bad_mask], expected_z[z_bad_mask], 3000)),
        material_violation_points=tuple(_sample_violation_array(center_xy_in[material_bad_mask], center_z_in[material_bad_mask], expected_z[material_bad_mask], 3000)),
        wall_points=tuple(_sample_xy_array(center_xy_in[wall_ok_mask], 5000)),
        wall_violation_points=tuple(_sample_xy_array(center_xy_in[wall_bad_mask], 3000)),
        triangle_colors=triangle_colors,
    )


def render_camotics_png(
    name: str,
    png_path: Path,
    stock_bounds: tuple[float, float, float, float],
    expected_regions: list[ExpectedSurfaceRegion],
    analysis: CamoticsAnalysis,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.0, 5.0), dpi=140)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(f"{name}\nCAMotics STL validation facets", fontsize=10)
    if analysis.exterior_stock_points:
        xs, ys = zip(*analysis.exterior_stock_points, strict=True)
        ax.scatter(xs, ys, s=1.0, c="#94a3b8", alpha=0.40, label="stock Z ok")
    if analysis.floor_points:
        xs, ys = zip(*analysis.floor_points, strict=True)
        ax.scatter(xs, ys, s=2.0, c="#2563eb", alpha=0.45, label="floor Z ok")
    if analysis.wall_points:
        xs, ys = zip(*analysis.wall_points, strict=True)
        ax.scatter(xs, ys, s=3.0, c="#16a34a", alpha=0.65, label="wall ok")
    if analysis.violation_points:
        xs = [point[0] for point in analysis.violation_points]
        ys = [point[1] for point in analysis.violation_points]
        ax.scatter(xs, ys, s=9, c="#dc2626", alpha=0.95, label="bad Z")
    if analysis.material_violation_points:
        xs = [point[0] for point in analysis.material_violation_points]
        ys = [point[1] for point in analysis.material_violation_points]
        ax.scatter(xs, ys, s=9, c="#dc2626", marker="^", alpha=0.95, label="bad material")
    if analysis.wall_violation_points:
        xs, ys = zip(*analysis.wall_violation_points, strict=True)
        ax.scatter(xs, ys, s=9, c="#dc2626", marker="x", alpha=0.95, label="bad wall")
    for region in expected_regions:
        for ring in _geometry_rings(region.geometry):
            ax.plot([point[0] for point in ring], [point[1] for point in ring], c="#2563eb", linewidth=0.8, linestyle="--", alpha=0.55)
    min_x, min_y, max_x, max_y = stock_bounds
    ax.plot([min_x, max_x, max_x, min_x, min_x], [min_y, min_y, max_y, max_y, min_y], c="#111827", linewidth=1.2)
    ax.text(
        0.01,
        0.01,
        (
            f"tri {analysis.triangles}  horiz {analysis.horizontal_facets}\n"
            f"Z bad {analysis.z_violating_facets}  material {analysis.material_violating_facets}  wall {analysis.wall_violating_facets}\n"
            f"expected regions {analysis.expected_region_count}"
        ),
        transform=ax.transAxes,
        fontsize=8,
        va="bottom",
        bbox={"facecolor": "white", "edgecolor": "#cbd5e1", "alpha": 0.9},
    )
    ax.legend(loc="upper right", fontsize=7, markerscale=3, frameon=True)
    ax.set_xlabel("X (in)")
    ax.set_ylabel("Y (in)")
    ax.grid(True, linewidth=0.3, color="#e5e7eb")
    fig.tight_layout()
    png_path.write_bytes(b"")
    fig.savefig(png_path)
    plt.close(fig)


def render_camotics_html(name: str, artifacts: CamoticsArtifacts, analysis: CamoticsAnalysis) -> None:
    payload = json.dumps(
        {
            "colors": analysis.triangle_colors,
        },
        separators=(",", ":"),
    )
    png_href = artifacts.png_path.name
    stl_href = artifacts.stl_path.name
    html = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8" />',
            '<meta name="viewport" content="width=device-width, initial-scale=1" />',
            f"<title>{escape(name)} CAMotics validation</title>",
            "<style>",
            _html_css(),
            "</style>",
            "</head>",
            "<body>",
            "<main>",
            f"<h1>{escape(name)} CAMotics validation</h1>",
            f'<p class="summary bad">bad Z {analysis.z_violating_facets}, bad material {analysis.material_violating_facets}, bad wall {analysis.wall_violating_facets}</p>',
            '<div class="grid">',
            '<article class="card"><h2>depth/wall map</h2>',
            f'<img src="{escape(png_href)}" alt="{escape(name)} validation map" />',
            "</article>",
            '<article class="card"><h2>interactive STL</h2>',
            f'<div class="stl-viewer" data-stl-id="stl-payload" data-stl-url="{escape(stl_href)}"></div>',
            '<p class="legend"><span><i class="floor"></i>floor</span><span><i class="wall"></i>wall ok</span><span><i class="bad"></i>bad</span><span><i class="stock"></i>stock/edge</span></p>',
            f'<p class="legend"><a href="{escape(stl_href)}">open STL</a></p>',
            "</article>",
            '<article class="card gcode"><h2>posted G-code</h2>',
            f"<pre>{escape(artifacts.nc_path.read_text(encoding='utf-8'))}</pre>",
            "</article>",
            "</div>",
            "</main>",
            f'<script type="application/json" id="stl-payload">{payload}</script>',
            '<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script>',
            '<script type="module">',
            _html_js(),
            "</script>",
            "</body>",
            "</html>",
        ]
    )
    artifacts.html_path.write_text(html + "\n", encoding="utf-8")


def _html_css() -> str:
    return """
:root { color-scheme: light; font-family: Arial, sans-serif; color: #111827; background: #f8fafc; }
body { margin: 0; }
main { padding: 20px; }
h1 { margin: 0 0 6px; font-size: 24px; }
.summary { margin: 0 0 16px; font-weight: 700; }
.bad { color: #dc2626; }
.grid { display: grid; grid-template-columns: minmax(420px, 0.8fr) minmax(520px, 1.2fr); gap: 14px; align-items: start; }
.card { background: #fff; border: 1px solid #d7dee8; border-radius: 8px; padding: 12px; min-width: 0; }
.card h2 { font-size: 15px; margin: 0 0 10px; }
.card img { width: 100%; height: 520px; object-fit: contain; display: block; }
.stl-viewer { width: 100%; height: 520px; background: #0f172a; border-radius: 6px; overflow: hidden; position: relative; }
.stl-viewer canvas { width: 100%; height: 100%; display: block; }
.stl-viewer::before { content: "loading STL"; position: absolute; inset: 0; display: grid; place-items: center; color: #cbd5e1; font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
.stl-viewer.loaded::before { display: none; }
.stl-viewer.error::before { content: attr(data-error); white-space: pre-line; padding: 18px; text-align: center; line-height: 1.45; text-transform: none; letter-spacing: 0; }
.legend { display: flex; gap: 10px; flex-wrap: wrap; margin: 8px 0 0; color: #475569; font-size: 12px; }
.legend span { display: inline-flex; gap: 4px; align-items: center; }
.legend i { width: 10px; height: 10px; display: inline-block; border-radius: 999px; }
.legend .floor { background: #2563eb; }
.legend .wall { background: #16a34a; }
.legend .bad { background: #dc2626; }
.legend .stock { background: #94a3b8; }
.gcode { grid-column: 1 / -1; }
pre { max-height: 340px; overflow: auto; font: 12px/1.35 Consolas, monospace; white-space: pre; }
@media (max-width: 1200px) { .grid { grid-template-columns: 1fr; } }
"""


def _html_js() -> str:
    return r"""
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

function base64ToBytes(base64) {
  const binary = atob(base64.replace(/\s+/g, ''));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}
function applyTriangleColors(geometry, colorsBase64) {
  const triangleColors = base64ToBytes(colorsBase64);
  const position = geometry.getAttribute('position');
  const triangleCount = Math.min(Math.floor(position.count / 3), Math.floor(triangleColors.length / 3));
  const colors = new Float32Array(position.count * 3);
  for (let triangle = 0; triangle < triangleCount; triangle += 1) {
    const r = triangleColors[triangle * 3] / 255;
    const g = triangleColors[triangle * 3 + 1] / 255;
    const b = triangleColors[triangle * 3 + 2] / 255;
    for (let vertex = 0; vertex < 3; vertex += 1) {
      const offset = (triangle * 3 + vertex) * 3;
      colors[offset] = r; colors[offset + 1] = g; colors[offset + 2] = b;
    }
  }
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
}
async function initViewer(container) {
  if (window.location.protocol === 'file:') {
    container.dataset.error = 'External STL preview needs this report served over http://localhost.\nRun scripts/serve_toolpath_report.py, then open the printed URL.';
    container.classList.add('error');
    return;
  }
  const payload = JSON.parse(document.getElementById(container.dataset.stlId).textContent);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f172a);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10000);
  camera.up.set(0, 0, 1);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = false;
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(0.5, -0.7, 1.1);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x93c5fd, 0.45);
  fill.position.set(-0.8, 0.8, 0.7);
  scene.add(fill);
  let geometry;
  try {
    const response = await fetch(container.dataset.stlUrl);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    geometry = new STLLoader().parse(await response.arrayBuffer());
  } catch (error) {
    container.dataset.error = `Failed to load STL preview.\n${error.message}`;
    container.classList.add('error');
    return;
  }
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  applyTriangleColors(geometry, payload.colors);
  const material = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.58, metalness: 0.04, side: THREE.DoubleSide, vertexColors: true });
  scene.add(new THREE.Mesh(geometry, material));
  const box = geometry.boundingBox.clone();
  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  box.getCenter(center); box.getSize(size);
  const radius = Math.max(size.x, size.y, size.z, 1);
  camera.position.set(radius * 0.9, -radius * 1.25, radius * 0.75);
  camera.near = Math.max(radius / 500, 0.01);
  camera.far = radius * 20;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  scene.add(new THREE.AxesHelper(radius * 0.3));
  function resize() {
    const rect = container.getBoundingClientRect();
    renderer.setSize(Math.max(1, rect.width), Math.max(1, rect.height), false);
    camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
    camera.updateProjectionMatrix();
  }
  function animate() {
    resize();
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }
  container.classList.add('loaded');
  animate();
}
document.querySelectorAll('.stl-viewer').forEach(initViewer);
"""


def _expected_surface_z_many(points, regions: list[ExpectedSurfaceRegion]) -> np.ndarray:
    expected_z = np.zeros(len(points), dtype=np.float64)
    for region in regions:
        covered = np.asarray(shapely.covers(region.geometry, points), dtype=bool)
        expected_z[covered] = np.minimum(expected_z[covered], region.depth_z)
    return expected_z


def _sorted_z_range_ids(mask: np.ndarray, z_values: np.ndarray, min_z: float, max_z: float) -> np.ndarray:
    ids = np.flatnonzero(mask)
    if len(ids) == 0:
        return ids
    order = ids[np.argsort(z_values[ids], kind="stable")]
    sorted_z = z_values[order]
    start = np.searchsorted(sorted_z, min_z, side="left")
    end = np.searchsorted(sorted_z, max_z, side="right")
    return order[start:end]


def _boundary_distance_many(points, regions: list[ExpectedSurfaceRegion]) -> np.ndarray:
    if not regions:
        return np.full(len(points), np.inf, dtype=np.float64)
    distances = [
        np.asarray(shapely.distance(region.geometry.boundary, points), dtype=np.float64)
        for region in regions
    ]
    return np.minimum.reduce(distances)


def _boundary_distance_many_numpy(
    point_xy: np.ndarray,
    regions: list[ExpectedSurfaceRegion],
    search_radius: float,
    *,
    bin_size: float | None = None,
) -> np.ndarray:
    segments = _boundary_segment_arrays(regions)
    if len(point_xy) == 0 or segments is None:
        return np.full(len(point_xy), np.inf, dtype=np.float32)
    x0, y0, x1, y1 = segments
    if bin_size is None:
        bin_size = max(search_radius * 2.0, 1e-3)
    min_x = min(float(point_xy[:, 0].min(initial=x0.min())), float(x0.min()), float(x1.min())) - search_radius
    min_y = min(float(point_xy[:, 1].min(initial=y0.min())), float(y0.min()), float(y1.min())) - search_radius
    bins = _boundary_segment_bins(x0, y0, x1, y1, search_radius, min_x, min_y, bin_size)
    result = np.full(len(point_xy), np.inf, dtype=np.float32)
    point_bin_x = np.floor((point_xy[:, 0] - min_x) / bin_size).astype(np.int32)
    point_bin_y = np.floor((point_xy[:, 1] - min_y) / bin_size).astype(np.int32)
    order = np.lexsort((point_bin_y, point_bin_x))
    sorted_x = point_bin_x[order]
    sorted_y = point_bin_y[order]
    if len(order) == 0:
        return result
    breaks = np.flatnonzero((sorted_x[1:] != sorted_x[:-1]) | (sorted_y[1:] != sorted_y[:-1])) + 1
    starts = np.concatenate(([0], breaks))
    ends = np.concatenate((breaks, [len(order)]))
    max_pairs = 2_000_000
    for start, end in zip(starts, ends, strict=True):
        key = (int(sorted_x[start]), int(sorted_y[start]))
        segment_ids = bins.get(key)
        if not segment_ids:
            continue
        segment_ids_array = np.asarray(segment_ids, dtype=np.int32)
        point_ids = order[start:end]
        point_chunk_size = max(1, max_pairs // max(1, len(segment_ids_array)))
        for chunk_start in range(0, len(point_ids), point_chunk_size):
            chunk_ids = point_ids[chunk_start:chunk_start + point_chunk_size]
            distance_sq = _point_segment_distance_sq_many(
                point_xy[chunk_ids],
                x0[segment_ids_array],
                y0[segment_ids_array],
                x1[segment_ids_array],
                y1[segment_ids_array],
            )
            min_distance_sq = distance_sq.min(axis=1, initial=np.inf)
            near = min_distance_sq <= search_radius * search_radius
            result[chunk_ids[near]] = np.sqrt(min_distance_sq[near]).astype(np.float32)
    return result


def _boundary_segment_arrays(regions: list[ExpectedSurfaceRegion]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    rings: list[list[tuple[float, float]]] = []
    for region in regions:
        rings.extend(_geometry_boundary_rings(region.geometry))
    starts: list[tuple[float, float]] = []
    ends: list[tuple[float, float]] = []
    for ring in rings:
        if len(ring) < 2:
            continue
        for start, end in zip(ring, ring[1:], strict=False):
            if math.hypot(end[0] - start[0], end[1] - start[1]) > 1e-12:
                starts.append(start)
                ends.append(end)
    if not starts:
        return None
    start_array = np.asarray(starts, dtype=np.float32)
    end_array = np.asarray(ends, dtype=np.float32)
    return start_array[:, 0], start_array[:, 1], end_array[:, 0], end_array[:, 1]


def _geometry_boundary_rings(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    if geometry.is_empty:
        return []
    if hasattr(geometry, "geoms"):
        rings: list[list[tuple[float, float]]] = []
        for child in geometry.geoms:
            rings.extend(_geometry_boundary_rings(child))
        return rings
    if hasattr(geometry, "exterior"):
        return [
            [(float(x), float(y)) for x, y in geometry.exterior.coords],
            *[[(float(x), float(y)) for x, y in interior.coords] for interior in geometry.interiors],
        ]
    if hasattr(geometry, "coords"):
        return [[(float(x), float(y)) for x, y in geometry.coords]]
    return []


def _boundary_segment_bins(
    x0: np.ndarray,
    y0: np.ndarray,
    x1: np.ndarray,
    y1: np.ndarray,
    padding: float,
    origin_x: float,
    origin_y: float,
    bin_size: float,
) -> dict[tuple[int, int], list[int]]:
    bins: dict[tuple[int, int], list[int]] = {}
    min_bin_x = np.floor((np.minimum(x0, x1) - padding - origin_x) / bin_size).astype(np.int32)
    max_bin_x = np.floor((np.maximum(x0, x1) + padding - origin_x) / bin_size).astype(np.int32)
    min_bin_y = np.floor((np.minimum(y0, y1) - padding - origin_y) / bin_size).astype(np.int32)
    max_bin_y = np.floor((np.maximum(y0, y1) + padding - origin_y) / bin_size).astype(np.int32)
    for segment_id in range(len(x0)):
        for bx in range(int(min_bin_x[segment_id]), int(max_bin_x[segment_id]) + 1):
            for by in range(int(min_bin_y[segment_id]), int(max_bin_y[segment_id]) + 1):
                bins.setdefault((bx, by), []).append(segment_id)
    return bins


def _point_segment_distance_sq_many(
    points: np.ndarray,
    x0: np.ndarray,
    y0: np.ndarray,
    x1: np.ndarray,
    y1: np.ndarray,
) -> np.ndarray:
    px = points[:, 0:1]
    py = points[:, 1:2]
    dx = x1[None, :] - x0[None, :]
    dy = y1[None, :] - y0[None, :]
    length_sq = dx * dx + dy * dy
    t = np.divide(
        (px - x0[None, :]) * dx + (py - y0[None, :]) * dy,
        length_sq,
        out=np.zeros((len(points), len(x0)), dtype=np.float32),
        where=length_sq > 1e-12,
    )
    t = np.clip(t, 0.0, 1.0)
    nearest_x = x0[None, :] + t * dx
    nearest_y = y0[None, :] + t * dy
    return (px - nearest_x) ** 2 + (py - nearest_y) ** 2


def _triangle_colors(
    count: int,
    *,
    exterior_stock_mask: np.ndarray,
    floor_mask: np.ndarray,
    edge_mask: np.ndarray,
    wall_ok_mask: np.ndarray,
    bad_visual_mask: np.ndarray,
) -> str:
    colors = np.tile(np.array([37, 99, 235], dtype=np.uint8), (count, 1))
    colors[exterior_stock_mask] = [148, 163, 184]
    colors[edge_mask] = [203, 213, 225]
    colors[floor_mask] = [37, 99, 235]
    colors[wall_ok_mask] = [22, 163, 74]
    colors[bad_visual_mask] = [220, 38, 38]
    return base64.b64encode(colors.tobytes()).decode("ascii")


def _strict_wall_validation_enabled() -> bool:
    return CAMOTICS_RESOLUTION_MM <= 0.762


def _expand_xy_mask(points: np.ndarray, seed_mask: np.ndarray, radius: float) -> np.ndarray:
    seed_points = points[seed_mask]
    if len(seed_points) == 0:
        return seed_mask.copy()
    radius_sq = radius * radius
    expanded = seed_mask.copy()
    chunk_size = 20000
    for start in range(0, len(points), chunk_size):
        chunk = points[start : start + chunk_size]
        distances_sq = ((chunk[:, None, :] - seed_points[None, :, :]) ** 2).sum(axis=2)
        expanded[start : start + chunk_size] |= distances_sq.min(axis=1) <= radius_sq
    return expanded


def _expand_xy_mask_numpy(points: np.ndarray, seed_mask: np.ndarray, radius: float) -> np.ndarray:
    seed_points = points[seed_mask]
    if len(seed_points) == 0:
        return seed_mask.copy()
    bin_size = max(radius, 1e-6)
    min_x = float(min(points[:, 0].min(), seed_points[:, 0].min())) - radius
    min_y = float(min(points[:, 1].min(), seed_points[:, 1].min())) - radius
    seed_bin_x = np.floor((seed_points[:, 0] - min_x) / bin_size).astype(np.int32)
    seed_bin_y = np.floor((seed_points[:, 1] - min_y) / bin_size).astype(np.int32)
    seed_bins: dict[tuple[int, int], list[int]] = {}
    for seed_index, key in enumerate(zip(seed_bin_x, seed_bin_y, strict=True)):
        seed_bins.setdefault((int(key[0]), int(key[1])), []).append(seed_index)

    point_bin_x = np.floor((points[:, 0] - min_x) / bin_size).astype(np.int32)
    point_bin_y = np.floor((points[:, 1] - min_y) / bin_size).astype(np.int32)
    order = np.lexsort((point_bin_y, point_bin_x))
    sorted_x = point_bin_x[order]
    sorted_y = point_bin_y[order]
    expanded = seed_mask.copy()
    if len(order) == 0:
        return expanded
    breaks = np.flatnonzero((sorted_x[1:] != sorted_x[:-1]) | (sorted_y[1:] != sorted_y[:-1])) + 1
    starts = np.concatenate(([0], breaks))
    ends = np.concatenate((breaks, [len(order)]))
    radius_sq = radius * radius
    max_pairs = 2_000_000
    for start, end in zip(starts, ends, strict=True):
        bx = int(sorted_x[start])
        by = int(sorted_y[start])
        candidate_seed_ids = [
            seed_id
            for nx in range(bx - 1, bx + 2)
            for ny in range(by - 1, by + 2)
            for seed_id in seed_bins.get((nx, ny), [])
        ]
        if not candidate_seed_ids:
            continue
        seed_ids = np.asarray(candidate_seed_ids, dtype=np.int32)
        point_ids = order[start:end]
        point_chunk_size = max(1, max_pairs // max(1, len(seed_ids)))
        for chunk_start in range(0, len(point_ids), point_chunk_size):
            chunk_ids = point_ids[chunk_start:chunk_start + point_chunk_size]
            deltas = points[chunk_ids, None, :] - seed_points[seed_ids][None, :, :]
            near = (deltas * deltas).sum(axis=2).min(axis=1, initial=np.inf) <= radius_sq
            expanded[chunk_ids[near]] = True
    return expanded


def _sample_xy_array(points: np.ndarray, limit: int) -> list[tuple[float, float]]:
    if len(points) == 0:
        return []
    stride = max(1, math.ceil(len(points) / limit))
    sampled = points[::stride][:limit]
    return [(float(point[0]), float(point[1])) for point in sampled]


def _sample_violation_array(points: np.ndarray, actual_z: np.ndarray, expected_z: np.ndarray, limit: int) -> list[tuple[float, float, float, float]]:
    if len(points) == 0:
        return []
    stride = max(1, math.ceil(len(points) / limit))
    sampled_points = points[::stride][:limit]
    sampled_actual = actual_z[::stride][:limit]
    sampled_expected = expected_z[::stride][:limit]
    return [
        (float(point[0]), float(point[1]), float(actual), float(expected))
        for point, actual, expected in zip(sampled_points, sampled_actual, sampled_expected, strict=True)
    ]


def _geometry_rings(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [[(float(x), float(y)) for x, y in geometry.exterior.coords]]
    return [ring for item in getattr(geometry, "geoms", []) for ring in _geometry_rings(item)]


def _tool_number(tool_id: str) -> int:
    match = re.search(r"\d+", tool_id)
    return int(match.group(0)) if match else 1


def _next_position(position: tuple[float, float, float], move) -> tuple[float, float, float]:
    return (
        float(move.x) if getattr(move, "x", None) is not None else position[0],
        float(move.y) if getattr(move, "y", None) is not None else position[1],
        float(move.z) if getattr(move, "z", None) is not None else position[2],
    )


def _depth_from_z(z: float) -> float:
    return max(0.0, -z)


def _arc_points(position: tuple[float, float, float], move: ArcMove) -> list[tuple[float, float, float]]:
    center_x = position[0] + move.i
    center_y = position[1] + move.j
    radius = math.hypot(position[0] - center_x, position[1] - center_y)
    if radius <= 1e-12:
        return [_next_position(position, move)]
    start_angle = math.atan2(position[1] - center_y, position[0] - center_x)
    end_angle = math.atan2(move.y - center_y, move.x - center_x)
    if move.direction == "ccw":
        sweep = (end_angle - start_angle) % (2 * math.pi)
        if sweep <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = 2 * math.pi
    else:
        sweep = -((start_angle - end_angle) % (2 * math.pi))
        if abs(sweep) <= 1e-12 and _same_xy(position, (move.x, move.y)):
            sweep = -2 * math.pi
    arc_length = abs(sweep) * radius
    steps = max(8, int(math.ceil(arc_length / max(radius / 8, 1e-6))))
    end_z = move.z if move.z is not None else position[2]
    points = []
    for index in range(1, steps + 1):
        fraction = index / steps
        angle = start_angle + sweep * fraction
        points.append(
            (
                center_x + math.cos(angle) * radius,
                center_y + math.sin(angle) * radius,
                position[2] + (end_z - position[2]) * fraction,
            )
        )
    points[-1] = (move.x, move.y, end_z)
    return points


def _same_xy(position: tuple[float, float, float], xy: tuple[float, float]) -> bool:
    return math.hypot(position[0] - xy[0], position[1] - xy[1]) <= 1e-9
