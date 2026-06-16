from __future__ import annotations

import base64
import math
import re
import shutil
import struct
import subprocess
from collections import Counter
from dataclasses import dataclass
from html import escape
import json
from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from dxfwiz.toolpaths import pocketing_cavalier as cavalier_pocketing
from dxfwiz.cam_kernel import cavalier
from dxfwiz.gcode import CanonicalMove, lift_gcode, validate_gcode_against_plan
from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import ContourOperation, HelicalPocketOperation, PocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove, SourceArcSegment, SourceLineSegment, SourcePath, ToolpathPass, ToolpathPlan
from dxfwiz.toolpaths.operations import source_path_points
from dxfwiz.toolpaths.pocketing_cavalier import (
    contour_operation_to_cavalier_toolpaths,
    helical_pocket_operation_to_cavalier_toolpaths,
    pocket_operation_to_cavalier_toolpaths,
)
from dxfwiz.toolpaths.posts.uccnc import UccncPost


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
REFERENCE_HTML = OUTPUT_DIR / "reference_operations_gcode_validation.html"
CAMOTICS_OUTPUT_DIR = OUTPUT_DIR / "camotics_reference"
CAMOTICS_RESOLUTION_MM = 1.016
CAMOTICS_EDGE_TOLERANCE_IN = max(0.015, CAMOTICS_RESOLUTION_MM * 3 / 25.4)
CAMOTICS_Z_TOLERANCE_IN = max(0.004, CAMOTICS_RESOLUTION_MM * 1.5 / 25.4)


@dataclass(frozen=True)
class ReferenceArtifacts:
    slug: str
    output_dir: Path
    nc_path: Path
    project_path: Path
    stl_path: Path
    png_path: Path


@dataclass(frozen=True)
class CamoticsAnalysis:
    triangles: int
    max_z_mm: float
    horizontal_facets: int
    checked_facets: int
    boundary_skipped_facets: int
    z_violating_facets: int
    wall_facets: int
    wall_checked_facets: int
    wall_violating_facets: int
    material_violating_facets: int
    expected_residual_facets: int
    expected_residual_area: float
    edge_tolerance_in: float
    z_tolerance_in: float
    exterior_stock_points: tuple[tuple[float, float], ...]
    expected_residual_points: tuple[tuple[float, float], ...]
    floor_points: tuple[tuple[float, float], ...]
    violation_points: tuple[tuple[float, float, float, float], ...]
    material_violation_points: tuple[tuple[float, float, float, float], ...]
    wall_points: tuple[tuple[float, float], ...]
    wall_violation_points: tuple[tuple[float, float], ...]
    triangle_colors: str


@dataclass(frozen=True)
class CamoticsMesh:
    triangles: int
    normals: np.ndarray
    vertices: np.ndarray
    centers: np.ndarray
    max_z_mm: float


@dataclass(frozen=True)
class ExpectedSurfaceRegion:
    geometry: BaseGeometry
    depth_z: float


@dataclass(frozen=True)
class ReferenceMetrics:
    lines: int
    moves: int
    rapid: int
    line: int
    arc: int
    cutting_length: float
    g1: int
    g2: int
    g3: int


@dataclass(frozen=True)
class ReferenceCase:
    name: str
    source_path: SourcePath
    passes: list[ToolpathPass]
    depth: float
    tool_diameter: float
    stepover_percent: float
    extra_source_paths: tuple[SourcePath, ...] = ()
    expected_geometry: BaseGeometry | None = None
    expected: ReferenceMetrics | None = None
    expected_wall_violations: bool = False
    expected_region_mode: str = "pocket"
    allow_duplicate_cut_motions: bool = False
    camotics: bool = True


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_reference_operations_render_gcode_gallery_and_match_expected_metrics():
    cases = _reference_cases()
    plans = [_plan_for_case(case) for case in cases]
    rendered = [(case, plan, UccncPost(precision=4).render(plan)) for case, plan in zip(cases, plans, strict=True)]

    camotics_artifacts = _write_camotics_reference_artifacts(rendered, CAMOTICS_OUTPUT_DIR)
    camotics_analyses = _generate_camotics_simulations(camotics_artifacts)
    html = _render_reference_html(rendered, REFERENCE_HTML, camotics_artifacts, camotics_analyses)

    assert "triangular cutout pocket" in html
    assert "circular helical pocket" in html
    assert "rounded rectangle with side bulge" in html
    assert "stl-viewer" in html
    assert set(camotics_artifacts) == {case.name for case in cases}
    cases_by_name = {case.name: case for case in cases}
    for case_name, artifact in camotics_artifacts.items():
        case = cases_by_name[case_name]
        assert artifact.output_dir.is_dir()
        gcode = artifact.nc_path.read_text(encoding="utf-8")
        assert "(" not in gcode
        assert ")" not in gcode
        assert "T1 M6" in gcode
        assert "T5" not in gcode
        assert artifact.project_path.exists()
        project = json.loads(artifact.project_path.read_text(encoding="utf-8"))
        assert project["units"] == "metric"
        assert project["tools"]["1"]["diameter"] == pytest.approx(case.tool_diameter)
        assert project["tools"]["1"]["shape"] == "cylindrical"
        assert project["files"] == [artifact.nc_path.name]
        if artifact.slug in camotics_analyses:
            analysis = camotics_analyses[artifact.slug]
            assert artifact.stl_path.exists()
            assert artifact.png_path.exists()
            assert analysis.triangles > 0
            if case.expected_wall_violations:
                assert analysis.z_violating_facets == 0
                assert _xy_spread(analysis.material_violation_points) <= 0.002
            elif case.expected_region_mode == "toolpath_sweep":
                assert analysis.z_violating_facets == 0
                assert analysis.material_violating_facets == 0
            else:
                assert analysis.z_violating_facets == 0
                assert analysis.material_violating_facets == 0
                if _strict_wall_validation_enabled():
                    assert analysis.wall_violating_facets == 0
            if analysis.expected_residual_area > (CAMOTICS_EDGE_TOLERANCE_IN * 2) ** 2:
                assert analysis.expected_residual_facets > 0
    for case, plan, gcode in rendered:
        issues = validate_gcode_against_plan(gcode, plan, safe_z=0.5)
        assert [issue for issue in issues if issue.severity == "error"] == [], case.name
        metrics = _metrics(gcode)
        if case.expected is not None:
            assert metrics == case.expected
        if not case.allow_duplicate_cut_motions:
            assert _duplicate_cut_motion_count(gcode) == 0, case.name
        assert metrics.moves > 0
        assert metrics.cutting_length > 0
        if case.name == "circular helical pocket":
            assert metrics.arc > metrics.line
        if case.name == "deep helical pocket with finish":
            rough_pass = next(toolpath_pass for toolpath_pass in plan.passes if toolpath_pass.kind == "helical_pocket")
            finish_pass = next(toolpath_pass for toolpath_pass in plan.passes if toolpath_pass.kind == "finish_contour")
            rough_rapid_entries = [
                move
                for move in rough_pass.moves
                if move.type == "rapid" and getattr(move, "x", None) is not None and getattr(move, "y", None) is not None
            ]
            rough_bottom_arcs = [
                move
                for move in rough_pass.moves
                if move.type == "arc" and getattr(move, "z", None) == pytest.approx(rough_pass.z_bottom)
            ]
            finish_xy_lines = [
                move
                for move in finish_pass.moves
                if move.type == "line" and (getattr(move, "x", None) is not None or getattr(move, "y", None) is not None)
            ]
            rough_arc_depths = {
                round(move.z, 4)
                for move in rough_pass.moves
                if move.type == "arc" and getattr(move, "z", None) is not None and move.z < 0
            }
            finish_arc_depths = {
                round(move.z, 4)
                for move in finish_pass.moves
                if move.type == "arc" and getattr(move, "z", None) is not None
            }
            rough_arc_radii_at_depth = {
                round(math.hypot(move.i, move.j), 4)
                for move in rough_pass.moves
                if move.type == "arc" and getattr(move, "z", None) == pytest.approx(rough_pass.z_bottom)
            }
            finish_arc_radii = {
                round(math.hypot(move.i, move.j), 4)
                for move in finish_pass.moves
                if move.type == "arc"
            }
            assert rough_bottom_arcs
            assert len(rough_rapid_entries) == 2
            assert {-0.25, -0.32}.issubset(rough_arc_depths)
            assert finish_arc_depths == {-0.32}
            assert 0.345 in rough_arc_radii_at_depth
            assert finish_arc_radii == {0.425}
            assert rough_pass.moves[-1].type == "rapid"
            assert finish_pass.moves[0].type == "rapid"
            assert finish_xy_lines == []
        if "square" in case.name:
            assert metrics.arc == 0


def _reference_cases() -> list[ReferenceCase]:
    tool = _tool(diameter=0.25, depth_per_pass=0.125)
    deep_tool = _tool(diameter=0.25, depth_per_pass=0.25)
    small_tool = _tool(diameter=0.125, depth_per_pass=0.0625)
    pocket = _pocket_operation("op-pocket", "entity", stepover_percent=80)
    vertical_pocket = _pocket_operation("op-pocket-vertical", "entity", stepover_percent=80, lead_in={"type": "line", "length": 0.4})
    circular = _helical_pocket_operation("op-circle", "circle")
    narrow_pocket = _pocket_operation("op-narrow", "narrow", stepover_percent=80, depth=0.125)
    external_contour = _external_contour_operation("op-external-rectangle", "external-rectangle")
    protrusion_contour = _external_contour_operation("op-round-corner-protrusion-contour", "round-corner-protrusion-contour")
    deep_helical = _helical_pocket_operation(
        "op-deep-helical-finish",
        "deep-helical",
        depth=0.32,
        pitch=0.055,
        roughing_depth_per_pass=0.25,
        roughing_side_allowance=0.08,
        finishing={"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
    )
    return [
        ReferenceCase(
            "triangular cutout pocket",
            _triangle_source_path("triangle"),
            pocket_operation_to_cavalier_toolpaths(pocket.model_copy(update={"id": "op-triangle", "entity": "triangle"}), _triangle_source_path("triangle"), tool, 0.5),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
            expected=ReferenceMetrics(lines=25, moves=14, rapid=3, line=11, arc=0, cutting_length=3.5752, g1=11, g2=0, g3=0),
        ),
        ReferenceCase(
            "circular helical pocket",
            _circle_source_path("circle", (0.0, 0.0), 0.55),
            helical_pocket_operation_to_cavalier_toolpaths(circular, _circle_source_path("circle", (0.0, 0.0), 0.55), tool, 0.5),
            depth=circular.depth,
            tool_diameter=tool.diameter,
            stepover_percent=circular.stepover_percent,
            expected=ReferenceMetrics(lines=27, moves=16, rapid=3, line=1, arc=12, cutting_length=7.9299, g1=1, g2=0, g3=12),
        ),
        ReferenceCase(
            "square rectangle pocket",
            _rectangle_source_path("square", 1.3, 0.9),
            pocket_operation_to_cavalier_toolpaths(pocket.model_copy(update={"id": "op-square", "entity": "square"}), _rectangle_source_path("square", 1.3, 0.9), tool, 0.5),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
            expected=ReferenceMetrics(lines=32, moves=21, rapid=3, line=18, arc=0, cutting_length=9.9078, g1=18, g2=0, g3=0),
        ),
        ReferenceCase(
            "rounded rectangle with side bulge",
            _rounded_bulged_rectangle_source_path("rounded-bulge"),
            pocket_operation_to_cavalier_toolpaths(
                pocket.model_copy(update={"id": "op-rounded-bulge", "entity": "rounded-bulge"}),
                _rounded_bulged_rectangle_source_path("rounded-bulge"),
                tool,
                0.5,
            ),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
            expected=ReferenceMetrics(lines=57, moves=46, rapid=3, line=25, arc=18, cutting_length=18.1432, g1=25, g2=8, g3=10),
            expected_wall_violations=True,
        ),
        ReferenceCase(
            "blobby cross pocket",
            _blobby_cross_source_path("blobby-cross"),
            pocket_operation_to_cavalier_toolpaths(
                pocket.model_copy(update={"id": "op-blobby-cross", "entity": "blobby-cross"}),
                _blobby_cross_source_path("blobby-cross"),
                small_tool,
                0.5,
            ),
            depth=pocket.depth,
            tool_diameter=small_tool.diameter,
            stepover_percent=pocket.stepover_percent,
        ),
        ReferenceCase(
            "four round corner pocket with north protrusion",
            _round_corner_protrusion_source_path("round-corner-protrusion"),
            pocket_operation_to_cavalier_toolpaths(
                vertical_pocket.model_copy(update={"id": "op-round-corner-protrusion", "entity": "round-corner-protrusion"}),
                _round_corner_protrusion_source_path("round-corner-protrusion"),
                small_tool,
                0.5,
            ),
            depth=vertical_pocket.depth,
            tool_diameter=small_tool.diameter,
            stepover_percent=vertical_pocket.stepover_percent,
        ),
        ReferenceCase(
            "four round corner north protrusion outside contour",
            _round_corner_protrusion_source_path("round-corner-protrusion-contour"),
            contour_operation_to_cavalier_toolpaths(
                protrusion_contour,
                _round_corner_protrusion_source_path("round-corner-protrusion-contour"),
                small_tool,
                0.5,
            ),
            depth=protrusion_contour.depth + protrusion_contour.extra_depth,
            tool_diameter=small_tool.diameter,
            stepover_percent=100,
            expected_region_mode="toolpath_sweep",
        ),
        _four_island_reference_case(pocket, small_tool),
        ReferenceCase(
            "arc-line capsule notch pocket",
            _capsule_notch_source_path("capsule-notch"),
            pocket_operation_to_cavalier_toolpaths(
                pocket.model_copy(update={"id": "op-capsule-notch", "entity": "capsule-notch"}),
                _capsule_notch_source_path("capsule-notch"),
                tool,
                0.5,
            ),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
        ),
        ReferenceCase(
            "dogbone with middle spike pocket",
            _dogbone_spike_source_path("dogbone-spike"),
            pocket_operation_to_cavalier_toolpaths(
                pocket.model_copy(update={"id": "op-dogbone-spike", "entity": "dogbone-spike"}),
                _dogbone_spike_source_path("dogbone-spike"),
                tool,
                0.5,
            ),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
        ),
        ReferenceCase(
            "long peninsula pocket",
            _peninsula_source_path("peninsula"),
            pocket_operation_to_cavalier_toolpaths(
                pocket.model_copy(update={"id": "op-peninsula", "entity": "peninsula"}),
                _peninsula_source_path("peninsula"),
                tool,
                0.5,
            ),
            depth=pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=pocket.stepover_percent,
        ),
        ReferenceCase(
            "barely wider than tool rectangle pocket",
            _rectangle_source_path("narrow", 1.05, 0.27),
            pocket_operation_to_cavalier_toolpaths(
                narrow_pocket,
                _rectangle_source_path("narrow", 1.05, 0.27),
                tool,
                0.5,
            ),
            depth=narrow_pocket.depth,
            tool_diameter=tool.diameter,
            stepover_percent=narrow_pocket.stepover_percent,
        ),
        ReferenceCase(
            "external rectangle contour with finish",
            _rectangle_source_path("external-rectangle", 1.2, 0.8),
            contour_operation_to_cavalier_toolpaths(
                external_contour,
                _rectangle_source_path("external-rectangle", 1.2, 0.8),
                tool,
                0.5,
            ),
            depth=external_contour.depth + external_contour.extra_depth,
            tool_diameter=tool.diameter,
            stepover_percent=100,
            expected_region_mode="toolpath_sweep",
        ),
        ReferenceCase(
            "deep helical pocket with finish",
            _circle_source_path("deep-helical", (0.0, 0.0), 0.55),
            helical_pocket_operation_to_cavalier_toolpaths(
                deep_helical,
                _circle_source_path("deep-helical", (0.0, 0.0), 0.55),
                deep_tool,
                0.5,
            ),
            depth=deep_helical.depth,
            tool_diameter=deep_tool.diameter,
            stepover_percent=deep_helical.stepover_percent,
        ),
    ]


def _plan_for_case(case: ReferenceCase) -> ToolpathPlan:
    return ToolpathPlan.model_validate(
        {
            "units": "in",
            "coordinate_system": "G55",
            "source_paths": [case.source_path.model_dump(), *[path.model_dump() for path in case.extra_source_paths]],
            "commands": [
                {"type": "comment", "text": f"reference operation: {case.name}"},
                {"type": "units", "length": "in"},
                {"type": "distance_mode", "mode": "absolute"},
                {"type": "plane", "plane": "xy"},
                {"type": "feed_mode", "mode": "units_per_min"},
                {"type": "coordinate_system", "code": "G55"},
                {"type": "tool_change", "tool": "t1"},
                {"type": "spindle_speed", "rpm": 18000},
                {"type": "spindle", "state": "on"},
                {"type": "rapid", "z": 0.5},
            ],
            "passes": _with_shutdown(case.passes),
        }
    )


def _with_shutdown(passes: list[ToolpathPass]) -> list[ToolpathPass]:
    if not passes:
        return []
    copied = [ToolpathPass.model_validate(toolpath_pass.model_dump()) for toolpath_pass in passes]
    moves = [*copied[-1].moves, {"type": "spindle", "state": "off"}, {"type": "program_end"}]
    copied[-1] = ToolpathPass.model_validate({**copied[-1].model_dump(), "moves": moves})
    return copied


def _render_reference_html(
    rendered: list[tuple[ReferenceCase, ToolpathPlan, str]],
    output_path: Path,
    artifacts: dict[str, ReferenceArtifacts],
    camotics_analyses: dict[str, CamoticsAnalysis],
) -> str:
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<title>dxfwiz reference operation validation</title>",
        "<style>",
        _reference_html_css(),
        "</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>dxfwiz reference operation validation</h1>",
        '<p class="lede">Each row shows generated paths, CAMotics material validation, an interactive STL preview, and the posted G-code.</p>',
    ]
    for index, (case, plan, gcode) in enumerate(rendered):
        metrics = _metrics(gcode)
        artifact = artifacts[case.name]
        analysis = camotics_analyses.get(artifact.slug)
        stl_id = f"stl-{index}"
        script = f'<script type="application/json" id="{stl_id}">{_stl_payload(artifact.stl_path, analysis)}</script>' if artifact.stl_path.exists() else ""
        lines.extend(
            [
                '<section class="case-row">',
                '<header class="case-header">',
                f"<h2>{escape(case.name)}</h2>",
                f'<p class="meta">{escape(_metrics_text(case, metrics))}</p>',
                "</header>",
                '<div class="grid">',
                _preview_card("top view", _inline_path_svg(case, plan, gcode, iso=False)),
                _preview_card("isometric view", _inline_path_svg(case, plan, gcode, iso=True)),
                _validation_card(artifact, analysis, output_path),
                _stl_card(artifact, stl_id),
                _gcode_card(gcode),
                "</div>",
                script,
                "</section>",
            ]
        )
    lines.extend(
        [
            "</main>",
            '<div class="lightbox" id="lightbox"><button type="button" aria-label="close">x</button><img alt="" /></div>',
            '<script type="importmap">',
            '{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}',
            "</script>",
            '<script type="module">',
            _reference_html_js(),
            "</script>",
            "</body>",
            "</html>",
        ]
    )
    svg = "\n".join(lines) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    return svg


def _write_camotics_reference_artifacts(
    rendered: list[tuple[ReferenceCase, ToolpathPlan, str]],
    output_dir: Path,
) -> dict[str, ReferenceArtifacts]:
    _reset_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, ReferenceArtifacts] = {}
    for case, _plan, gcode in rendered:
        slug = _slug(case.name)
        case_dir = output_dir / slug
        case_dir.mkdir(parents=True, exist_ok=True)
        artifact = ReferenceArtifacts(
            slug=slug,
            output_dir=case_dir,
            nc_path=case_dir / "generated.nc",
            project_path=case_dir / "project.camotics",
            stl_path=case_dir / "simulated.stl",
            png_path=case_dir / "simulated.png",
        )
        artifact.nc_path.write_text(_camotics_gcode(gcode), encoding="utf-8")
        artifact.project_path.write_text(
            json.dumps(_camotics_project(case, artifact.nc_path.name), indent=2) + "\n",
            encoding="utf-8",
        )
        artifacts[case.name] = artifact
    return artifacts


def _preview_card(title: str, svg: str) -> str:
    return f'<article class="card preview-card"><h3>{escape(title)}</h3>{svg}</article>'


def _validation_card(artifact: ReferenceArtifacts, analysis: CamoticsAnalysis | None, report_path: Path) -> str:
    href = artifact.png_path.relative_to(report_path.parent).as_posix()
    if analysis is None or not artifact.png_path.exists():
        summary = "CAMotics not found; validation skipped."
        image = '<div class="validation-placeholder">no material simulation</div>'
    else:
        status = "ok" if analysis.z_violating_facets == 0 and analysis.material_violating_facets == 0 and analysis.wall_violating_facets == 0 else "bad"
        summary = (
            f'<span class="{status}">bad Z {analysis.z_violating_facets}, bad material {analysis.material_violating_facets}, bad wall {analysis.wall_violating_facets}</span>'
            f"<br />Z checked {analysis.checked_facets}, wall checked {analysis.wall_checked_facets}, "
            f"expected residual {analysis.expected_residual_facets}"
            f"<br />tol wall/edge {analysis.edge_tolerance_in:.3f} in, Z {analysis.z_tolerance_in:.3f} in"
        )
        image = f'<img class="lightboxable validation-img" src="{escape(href)}" alt="{escape(artifact.slug)} validation map" />'
    return (
        '<article class="card validation-card">'
        "<h3>depth/wall map</h3>"
        f"{image}"
        f'<p class="meta">{summary}</p>'
        "</article>"
    )


def _stl_card(artifact: ReferenceArtifacts, stl_id: str) -> str:
    if not artifact.stl_path.exists():
        return (
            '<article class="card stl-card">'
            "<h3>interactive STL</h3>"
            '<div class="stl-placeholder">CAMotics STL not generated for this reference case.</div>'
            '<p class="meta">Vector paths and posted G-code are still shown for human review.</p>'
            "</article>"
        )
    return (
        '<article class="card stl-card">'
        "<h3>interactive STL</h3>"
        f'<div class="stl-viewer" data-stl-id="{escape(stl_id)}" aria-label="{escape(artifact.slug)} STL viewer"></div>'
        '<p class="meta stl-legend">'
        '<span><i class="floor"></i>floor</span>'
        '<span><i class="wall"></i>wall ok</span>'
        '<span><i class="residual"></i>expected residual</span>'
        '<span><i class="bad"></i>bad</span>'
        '<span><i class="stock"></i>stock/edge</span>'
        "</p>"
        '<p class="meta">Three.js full-STL preview with validation colors. Drag to rotate, wheel to zoom, right-drag or shift-drag to pan.</p>'
        "</article>"
    )


def _gcode_card(gcode: str) -> str:
    return (
        '<article class="card gcode-card">'
        "<h3>posted G-code</h3>"
        f"<pre>{escape(gcode)}</pre>"
        "</article>"
    )


def _inline_path_svg(case: ReferenceCase, plan: ToolpathPlan, gcode: str, *, iso: bool) -> str:
    width = 330
    height = 300
    content = "\n".join(_path_view([case.source_path, *case.extra_source_paths], plan, gcode, 14, 14, width - 28, height - 28, iso=iso))
    return "\n".join(
        [
            f'<svg class="path-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            "<defs>",
            '<marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto" markerUnits="strokeWidth">',
            '<path d="M0,0 L0,6 L7,3 z" fill="#2563eb" />',
            "</marker>",
            "</defs>",
            content,
            "</svg>",
        ]
    )


def _stl_payload(stl_path: Path, analysis: CamoticsAnalysis | None) -> str:
    return json.dumps(
        {
            "stl": base64.b64encode(stl_path.read_bytes()).decode("ascii"),
            "colors": analysis.triangle_colors if analysis is not None else None,
        },
        separators=(",", ":"),
    )


def _reference_html_css() -> str:
    return """
:root { color-scheme: light; font-family: Arial, sans-serif; color: #111827; background: #f8fafc; }
body { margin: 0; }
main { padding: 24px; max-width: 2140px; margin: 0 auto; }
h1 { margin: 0 0 6px; font-size: 28px; }
.lede { margin: 0 0 22px; color: #475569; }
.case-row { background: white; border: 1px solid #d7dee8; border-radius: 10px; margin: 0 0 20px; padding: 18px; }
.case-header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 12px; }
.case-header h2 { font-size: 21px; margin: 0; }
.meta { color: #475569; font-size: 12px; line-height: 1.35; }
.grid { display: grid; grid-template-columns: 360px 360px 460px 460px minmax(420px, 1fr); gap: 14px; align-items: start; }
.card { border: 1px solid #d7dee8; border-radius: 8px; background: #fff; padding: 12px; min-width: 0; }
.card h3 { margin: 0 0 8px; font-size: 14px; }
.path-svg { width: 100%; height: 300px; display: block; background: #fff; }
.source { fill: none; stroke: #111827; stroke-width: 2.2; }
.cut { fill: none; stroke: #7c3aed; stroke-width: 2.4; }
.finish { fill: none; stroke: #dc2626; stroke-width: 2.5; }
.rapid { fill: none; stroke: #94a3b8; stroke-width: 1.5; stroke-dasharray: 5 5; }
.arrow { stroke: #2563eb; stroke-width: 2; }
.dot { fill: #ffffff; stroke: #2563eb; stroke-width: 1.4; }
.start { fill: #16a34a; stroke: none; }
.validation-img { width: 100%; height: 300px; object-fit: contain; cursor: zoom-in; background: #fff; }
.validation-placeholder, .stl-placeholder { height: 300px; display: grid; place-items: center; background: #f1f5f9; color: #64748b; border-radius: 6px; font-size: 13px; text-align: center; padding: 0 18px; }
.stl-viewer { width: 100%; height: 300px; display: block; background: #0f172a; border-radius: 6px; cursor: grab; overflow: hidden; position: relative; }
.stl-viewer canvas { width: 100%; height: 100%; display: block; }
.stl-viewer:active { cursor: grabbing; }
.stl-viewer::before { content: "loading STL"; position: absolute; inset: 0; display: grid; place-items: center; color: #cbd5e1; font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
.stl-viewer.loaded::before { display: none; }
.stl-legend { display: flex; gap: 10px; flex-wrap: wrap; margin: 8px 0 4px; }
.stl-legend span { display: inline-flex; align-items: center; gap: 4px; }
.stl-legend i { width: 10px; height: 10px; border-radius: 999px; display: inline-block; }
.stl-legend .floor { background: #2563eb; }
.stl-legend .wall { background: #16a34a; }
.stl-legend .residual { background: #84cc16; }
.stl-legend .bad { background: #dc2626; }
.stl-legend .stock { background: #94a3b8; }
.gcode-card { grid-column: span 1; }
.gcode-card pre { margin: 0; max-height: 350px; overflow: auto; font: 12px/1.35 Consolas, monospace; white-space: pre; }
.ok { color: #15803d; font-weight: 700; }
.bad { color: #dc2626; font-weight: 700; }
.lightbox { display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.82); align-items: center; justify-content: center; z-index: 10; }
.lightbox.open { display: flex; }
.lightbox img { max-width: 96vw; max-height: 94vh; background: white; border-radius: 8px; }
.lightbox button { position: fixed; top: 18px; right: 22px; border: 0; border-radius: 999px; width: 34px; height: 34px; font-size: 20px; cursor: pointer; }
@media (max-width: 1700px) { .grid { grid-template-columns: repeat(2, minmax(340px, 1fr)); } .gcode-card { grid-column: 1 / -1; } }
"""


def _reference_html_js() -> str:
    return r"""
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

const lightbox = document.getElementById('lightbox');
const lightboxImage = lightbox.querySelector('img');
document.querySelectorAll('.lightboxable').forEach(img => {
  img.addEventListener('click', () => {
    lightboxImage.src = img.src;
    lightbox.classList.add('open');
  });
});
lightbox.addEventListener('click', event => {
  if (event.target === lightbox || event.target.tagName === 'BUTTON') lightbox.classList.remove('open');
});

function base64ToBytes(base64) {
  const binary = atob(base64.replace(/\s+/g, ''));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function base64ToArrayBuffer(base64) {
  return base64ToBytes(base64).buffer;
}

function applyTriangleColors(geometry, colorsBase64) {
  if (!colorsBase64) return false;
  const triangleColors = base64ToBytes(colorsBase64);
  const position = geometry.getAttribute('position');
  const triangleCount = Math.min(Math.floor(position.count / 3), Math.floor(triangleColors.length / 3));
  if (triangleCount <= 0) return false;
  const colors = new Float32Array(position.count * 3);
  for (let triangle = 0; triangle < triangleCount; triangle += 1) {
    const r = triangleColors[triangle * 3] / 255;
    const g = triangleColors[triangle * 3 + 1] / 255;
    const b = triangleColors[triangle * 3 + 2] / 255;
    for (let vertex = 0; vertex < 3; vertex += 1) {
      const offset = (triangle * 3 + vertex) * 3;
      colors[offset] = r;
      colors[offset + 1] = g;
      colors[offset + 2] = b;
    }
  }
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  return true;
}

function fitCamera(camera, controls, box) {
  const center = new THREE.Vector3();
  const size = new THREE.Vector3();
  box.getCenter(center);
  box.getSize(size);
  const radius = Math.max(size.x, size.y, size.z, 1);
  camera.up.set(0, 0, 1);
  camera.position.set(radius * 0.9, -radius * 1.25, radius * 0.75);
  camera.near = Math.max(radius / 500, 0.01);
  camera.far = radius * 20;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
}

function initViewer(container) {
  if (container.dataset.ready === '1') return;
  container.dataset.ready = '1';

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

  const loader = new STLLoader();
  const payload = JSON.parse(document.getElementById(container.dataset.stlId).textContent);
  const geometry = loader.parse(base64ToArrayBuffer(payload.stl));
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  const hasValidationColors = applyTriangleColors(geometry, payload.colors);

  const material = new THREE.MeshStandardMaterial({
    color: hasValidationColors ? 0xffffff : 0x2563eb,
    roughness: 0.58,
    metalness: 0.04,
    side: THREE.DoubleSide,
    vertexColors: hasValidationColors,
  });
  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  const box = geometry.boundingBox.clone();
  fitCamera(camera, controls, box);

  const axes = new THREE.AxesHelper(Math.max(box.max.x - box.min.x, box.max.y - box.min.y, box.max.z - box.min.z) * 0.3);
  axes.position.copy(controls.target);
  scene.add(axes);

  function resize() {
    const rect = container.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width));
    const height = Math.max(1, Math.floor(rect.height));
    if (renderer.domElement.width !== Math.floor(width * renderer.getPixelRatio()) || renderer.domElement.height !== Math.floor(height * renderer.getPixelRatio())) {
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
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

const viewers = [...document.querySelectorAll('.stl-viewer')];
if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      initViewer(entry.target);
      observer.unobserve(entry.target);
    });
  }, { rootMargin: '500px 0px' });
  viewers.forEach(viewer => observer.observe(viewer));
} else {
  viewers.forEach(initViewer);
}
"""


def _reset_output_dir(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _camotics_gcode(gcode: str) -> str:
    lines = []
    for line in gcode.splitlines():
        line = re.sub(r"\([^)]*\)", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "reference"


def _camotics_project(case: ReferenceCase, gcode_name: str) -> dict:
    min_x, min_y, max_x, max_y = _source_bounds(case.source_path)
    margin = max(case.tool_diameter * 1.75, 0.1)
    stock_bottom = -(case.depth + margin)
    scale = 25.4
    return {
        "units": "metric",
        "resolution-mode": "high",
        "resolution": CAMOTICS_RESOLUTION_MM,
        "tools": {
            "1": {
                "units": "imperial",
                "shape": "cylindrical",
                "length": 1.0,
                "diameter": case.tool_diameter,
                "description": f"{case.tool_diameter:g} inch flat end mill",
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


def _find_camsim() -> Path | None:
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


def _generate_camotics_simulations(artifacts: dict[str, ReferenceArtifacts]) -> dict[str, CamoticsAnalysis]:
    camsim = _find_camsim()
    if camsim is None:
        return {}
    analyses: dict[str, CamoticsAnalysis] = {}
    for case in _reference_cases():
        if not case.camotics:
            continue
        artifact = artifacts[case.name]
        subprocess.run(
            [
                str(camsim),
                "--resolution",
                str(CAMOTICS_RESOLUTION_MM),
                "--threads",
                "4",
                artifact.project_path.name,
                artifact.stl_path.name,
            ],
            cwd=artifact.output_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        mesh = _load_camotics_stl(artifact.stl_path)
        analysis = _analyze_camotics_stl(case, mesh)
        _render_camotics_png(case, artifact.png_path, analysis)
        analyses[artifact.slug] = analysis
    return analyses


def _load_camotics_stl(stl_path: Path) -> CamoticsMesh:
    data = stl_path.read_bytes()
    if len(data) < 84:
        raise AssertionError(f"{stl_path} is too small to be a binary STL")
    triangles = struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + triangles * 50
    if len(data) < expected_size:
        raise AssertionError(f"{stl_path} is truncated")
    dtype = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    records = np.frombuffer(data, dtype=dtype, count=triangles, offset=84)
    vertices = records["vertices"].astype(np.float64, copy=False)
    centers = vertices.mean(axis=1)
    return CamoticsMesh(
        triangles=triangles,
        normals=records["normal"].astype(np.float64, copy=False),
        vertices=vertices,
        centers=centers,
        max_z_mm=float(vertices[:, :, 2].max()),
    )


def _analyze_camotics_stl(case: ReferenceCase, mesh: CamoticsMesh) -> CamoticsAnalysis:
    horizontal_z_tolerance_mm = max(0.20, CAMOTICS_RESOLUTION_MM * 1.1)
    source_geometry = _source_geometry(case)
    expected_regions = _expected_surface_regions(case)

    center_xy_in = mesh.centers[:, :2] / 25.4
    center_z_in = mesh.centers[:, 2] / 25.4
    normal_z = mesh.normals[:, 2]
    points = shapely.points(center_xy_in[:, 0], center_xy_in[:, 1])
    source_covers = np.asarray(shapely.covers(source_geometry, points), dtype=bool)
    expected_z = _expected_surface_z_many(points, expected_regions)
    boundary_distance = _boundary_distance_many(points, expected_regions)

    z_span_mm = np.ptp(mesh.vertices[:, :, 2], axis=1)
    horizontal_mask = (np.abs(normal_z) >= 0.75) & (z_span_mm <= horizontal_z_tolerance_mm)
    stock_underside_mask = (expected_z < -1e-9) & (normal_z < -0.75) & (center_z_in < expected_z - CAMOTICS_Z_TOLERANCE_IN)
    horizontal_mask &= ~stock_underside_mask
    horizontal_mask &= ~((expected_z >= -1e-9) & (normal_z < 0.75))
    edge_mask = horizontal_mask & (boundary_distance <= CAMOTICS_EDGE_TOLERANCE_IN)
    check_mask = horizontal_mask & ~edge_mask
    floor_mask = check_mask & (expected_z < -1e-9)
    expected_residual_mask = check_mask & (expected_z >= -1e-9) & source_covers
    exterior_stock_mask = check_mask & (expected_z >= -1e-9) & ~source_covers
    z_bad_mask = check_mask & (np.abs(center_z_in - expected_z) > CAMOTICS_Z_TOLERANCE_IN)
    material_bad_mask = (
        (expected_z < -1e-9)
        & (center_z_in > expected_z + CAMOTICS_Z_TOLERANCE_IN)
        & (center_z_in <= CAMOTICS_Z_TOLERANCE_IN)
        & (boundary_distance > CAMOTICS_EDGE_TOLERANCE_IN)
    )

    wall_search_tolerance = max(CAMOTICS_EDGE_TOLERANCE_IN * 3, 0.06)
    min_depth = min((region.depth_z for region in expected_regions), default=0.0)
    vertical_candidate_mask = (
        (np.abs(normal_z) <= 0.25)
        & (center_z_in <= CAMOTICS_Z_TOLERANCE_IN)
        & (center_z_in >= min_depth - CAMOTICS_Z_TOLERANCE_IN)
    )
    relevant_wall_mask = source_covers | (expected_z < -1e-9) | (boundary_distance <= wall_search_tolerance)
    vertical_mask = vertical_candidate_mask & relevant_wall_mask
    wall_ok_mask = vertical_mask & (boundary_distance <= CAMOTICS_EDGE_TOLERANCE_IN)
    wall_bad_mask = vertical_mask & ~wall_ok_mask
    if not _strict_wall_validation_enabled():
        wall_ok_mask = vertical_mask
        wall_bad_mask = np.zeros(len(vertical_mask), dtype=bool)
    bad_visual_mask = _expand_xy_mask(
        center_xy_in,
        z_bad_mask | material_bad_mask | wall_bad_mask,
        CAMOTICS_EDGE_TOLERANCE_IN,
    )
    triangle_colors = _triangle_colors(
        len(center_xy_in),
        exterior_stock_mask=exterior_stock_mask,
        expected_residual_mask=expected_residual_mask,
        floor_mask=floor_mask,
        edge_mask=edge_mask,
        wall_ok_mask=wall_ok_mask,
        bad_visual_mask=bad_visual_mask,
    )

    return CamoticsAnalysis(
        triangles=mesh.triangles,
        max_z_mm=mesh.max_z_mm,
        horizontal_facets=int(horizontal_mask.sum()),
        checked_facets=int(check_mask.sum()),
        boundary_skipped_facets=int(edge_mask.sum()),
        z_violating_facets=int(z_bad_mask.sum()),
        wall_facets=int(vertical_mask.sum()),
        wall_checked_facets=int(wall_ok_mask.sum()),
        wall_violating_facets=int(wall_bad_mask.sum()),
        material_violating_facets=int(material_bad_mask.sum()),
        expected_residual_facets=int(expected_residual_mask.sum()),
        expected_residual_area=float(source_geometry.difference(_union_expected_geometries(expected_regions)).area),
        edge_tolerance_in=CAMOTICS_EDGE_TOLERANCE_IN,
        z_tolerance_in=CAMOTICS_Z_TOLERANCE_IN,
        exterior_stock_points=tuple(_sample_xy_array(center_xy_in[exterior_stock_mask], 6000)),
        expected_residual_points=tuple(_sample_xy_array(center_xy_in[expected_residual_mask], 4000)),
        floor_points=tuple(_sample_xy_array(center_xy_in[floor_mask], 6000)),
        violation_points=tuple(_sample_violation_array(center_xy_in[z_bad_mask], center_z_in[z_bad_mask], expected_z[z_bad_mask], 3000)),
        material_violation_points=tuple(_sample_violation_array(center_xy_in[material_bad_mask], center_z_in[material_bad_mask], expected_z[material_bad_mask], 3000)),
        wall_points=tuple(_sample_xy_array(center_xy_in[wall_ok_mask], 5000)),
        wall_violation_points=tuple(_sample_xy_array(center_xy_in[wall_bad_mask], 3000)),
        triangle_colors=triangle_colors,
    )


def _render_camotics_png(case: ReferenceCase, png_path: Path, analysis: CamoticsAnalysis) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    polygon = _source_polygon(case.source_path)
    fig, ax = plt.subplots(figsize=(5.6, 4.2), dpi=120)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(f"{case.name}\nCAMotics STL horizontal facets", fontsize=10)
    if analysis.exterior_stock_points:
        xs, ys = zip(*analysis.exterior_stock_points, strict=True)
        ax.scatter(xs, ys, s=1.3, c="#94a3b8", alpha=0.45, label="stock Z ok")
    if analysis.expected_residual_points:
        xs, ys = zip(*analysis.expected_residual_points, strict=True)
        ax.scatter(xs, ys, s=4.0, c="#84cc16", alpha=0.80, label="expected residual")
    if analysis.floor_points:
        xs, ys = zip(*analysis.floor_points, strict=True)
        ax.scatter(xs, ys, s=3.0, c="#2563eb", alpha=0.55, label="floor Z ok")
    if analysis.wall_points:
        xs, ys = zip(*analysis.wall_points, strict=True)
        ax.scatter(xs, ys, s=4, c="#16a34a", alpha=0.70, label="wall ok")
    if analysis.violation_points:
        xs = [point[0] for point in analysis.violation_points]
        ys = [point[1] for point in analysis.violation_points]
        ax.scatter(xs, ys, s=10, c="#dc2626", alpha=0.95, label="bad Z")
    if analysis.material_violation_points:
        xs = [point[0] for point in analysis.material_violation_points]
        ys = [point[1] for point in analysis.material_violation_points]
        ax.scatter(xs, ys, s=10, c="#dc2626", marker="^", alpha=0.95, label="bad material")
    if analysis.wall_violation_points:
        xs, ys = zip(*analysis.wall_violation_points, strict=True)
        ax.scatter(xs, ys, s=10, c="#dc2626", marker="x", alpha=0.95, label="bad wall")
    boundary = [*polygon, polygon[0]]
    ax.plot([point[0] for point in boundary], [point[1] for point in boundary], c="#111827", linewidth=1.6)
    for ring in _expected_region_rings(case):
        ax.plot([point[0] for point in ring], [point[1] for point in ring], c="#2563eb", linewidth=1.2, linestyle="--", alpha=0.85)
    ax.text(
        0.01,
        0.01,
        (
            f"tri {analysis.triangles}  horiz {analysis.horizontal_facets}\n"
            f"Z checked {analysis.checked_facets}  edge {analysis.boundary_skipped_facets}  bad {analysis.z_violating_facets}\n"
            f"wall ok {analysis.wall_checked_facets}  bad {analysis.wall_violating_facets}  material {analysis.material_violating_facets}"
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
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)


def _source_bounds(source_path: SourcePath) -> tuple[float, float, float, float]:
    points = source_path_points(source_path, arc_segments=96)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _source_polygon(source_path: SourcePath) -> list[tuple[float, float]]:
    points = source_path_points(source_path, arc_segments=96)
    if len(points) > 1 and _same_xy(points[0], points[-1]):
        points = points[:-1]
    return points


def _source_geometry(case: ReferenceCase) -> BaseGeometry:
    if case.expected_geometry is not None:
        return case.expected_geometry
    if case.expected_region_mode == "toolpath_sweep":
        swept = _case_toolpath_sweep_geometry(case)
        if not swept.is_empty:
            return swept
    source = Polygon(_source_polygon(case.source_path))
    if not source.is_valid:
        source = source.buffer(0)
    return source


def _expected_region_rings(case: ReferenceCase) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    for region in _expected_surface_regions(case):
        geometries = list(region.geometry.geoms) if hasattr(region.geometry, "geoms") else [region.geometry]
        for geometry in geometries:
            if hasattr(geometry, "exterior"):
                rings.append([(float(x), float(y)) for x, y in geometry.exterior.coords])
                rings.extend([[(float(x), float(y)) for x, y in interior.coords] for interior in geometry.interiors])
    return rings


def _expected_surface_regions(case: ReferenceCase) -> list[ExpectedSurfaceRegion]:
    if case.expected_region_mode == "toolpath_sweep":
        sweep = _case_toolpath_sweep_geometry(case)
        if sweep.is_empty:
            return []
        return [ExpectedSurfaceRegion(sweep, -case.depth)]
    source = _source_geometry(case)
    cutter_accessible_floor = source.buffer(-case.tool_diameter / 2, join_style=1).buffer(case.tool_diameter / 2, join_style=1)
    if cutter_accessible_floor.is_empty:
        return []
    return [ExpectedSurfaceRegion(cutter_accessible_floor, -case.depth)]


def _case_toolpath_sweep_geometry(case: ReferenceCase) -> BaseGeometry:
    swept_regions = [
        cavalier_pocketing._bottom_depth_swept_material(toolpath_pass.moves, toolpath_pass.z_bottom, case.tool_diameter / 2)
        for toolpath_pass in case.passes
        if toolpath_pass.kind in {"rough_contour", "finish_contour", "pocket_clear", "pocket_floor_finish", "pocket_wall_finish", "helical_pocket"}
    ]
    swept_regions = [region for region in swept_regions if not region.is_empty]
    if not swept_regions:
        return Polygon()
    return shapely.union_all(swept_regions)


def _union_expected_geometries(regions: list[ExpectedSurfaceRegion]) -> BaseGeometry:
    if not regions:
        return Polygon()
    return shapely.union_all([region.geometry for region in regions])


def _expected_surface_z_many(points, regions: list[ExpectedSurfaceRegion]) -> np.ndarray:
    expected_z = np.zeros(len(points), dtype=np.float64)
    for region in regions:
        covered = np.asarray(shapely.covers(region.geometry, points), dtype=bool)
        expected_z[covered] = np.minimum(expected_z[covered], region.depth_z)
    return expected_z


def _boundary_distance_many(points, regions: list[ExpectedSurfaceRegion]) -> np.ndarray:
    if not regions:
        return np.full(len(points), np.inf, dtype=np.float64)
    distances = [
        np.asarray(shapely.distance(region.geometry.boundary, points), dtype=np.float64)
        for region in regions
    ]
    return np.minimum.reduce(distances)


def _triangle_colors(
    count: int,
    *,
    exterior_stock_mask: np.ndarray,
    expected_residual_mask: np.ndarray,
    floor_mask: np.ndarray,
    edge_mask: np.ndarray,
    wall_ok_mask: np.ndarray,
    bad_visual_mask: np.ndarray,
) -> str:
    colors = np.tile(np.array([37, 99, 235], dtype=np.uint8), (count, 1))
    colors[exterior_stock_mask] = [148, 163, 184]
    colors[edge_mask] = [203, 213, 225]
    colors[floor_mask] = [37, 99, 235]
    colors[expected_residual_mask] = [132, 204, 22]
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


def _same_xy(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return abs(first[0] - second[0]) <= 1e-9 and abs(first[1] - second[1]) <= 1e-9


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


def _path_view(
    source_paths: list[SourcePath],
    plan: ToolpathPlan,
    gcode: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    iso: bool,
) -> list[str]:
    sources = [
        [(_project((px, py, 0.0), iso), "source") for px, py in source_path_points(source_path, arc_segments=48)]
        for source_path in source_paths
    ]
    moves = [move for move in lift_gcode(gcode).moves if move.start is not None and move.end is not None]
    motion_styles = _motion_styles_for_plan(plan)
    cut_segments: list[tuple[str, list[tuple[float, float]]]] = []
    rapid_segments: list[list[tuple[float, float]]] = []
    for index, move in enumerate(moves):
        points = [_project(point, iso) for point in _move_points(move)]
        if len(points) < 2:
            continue
        if move.kind == "rapid":
            rapid_segments.append(points)
        else:
            css = motion_styles[index] if index < len(motion_styles) else "cut"
            cut_segments.append((css, points))
    all_points = [
        point
        for source in sources
        for point, _kind in source
    ] + [point for _css, segment in cut_segments for point in segment] + [point for segment in rapid_segments for point in segment]
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    scale = min(width / max(max_x - min_x, 1e-9), height / max(max_y - min_y, 1e-9))

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        return x + (point[0] - min_x) * scale, y + (max_y - point[1]) * scale

    lines = []
    for source in sources:
        source_points = [f"{screen(point)[0]:.2f},{screen(point)[1]:.2f}" for point, _kind in source]
        if source_points:
            source_points.append(source_points[0])
            lines.append(f'<polyline class="source" points="{" ".join(source_points)}" />')
    screened_segments = [
        *[("rapid", [screen(point) for point in points]) for points in rapid_segments],
        *[(css, [screen(point) for point in points]) for css, points in cut_segments],
    ]
    duplicate_counts = Counter(_coincident_key(points) for css, points in screened_segments if css in {"cut", "finish"}) if not iso else Counter()
    duplicate_seen: Counter[tuple[tuple[int, int], ...]] = Counter()
    dot_points: list[tuple[float, float]] = []
    for css, screen_points in screened_segments:
        lines.append(f'<polyline class="{css}" points="{" ".join(f"{px:.2f},{py:.2f}" for px, py in screen_points)}" />')
        if css in {"cut", "finish"}:
            draw_points = screen_points
            if not iso:
                key = _coincident_key(screen_points)
                draw_points = _fanout_points(screen_points, duplicate_seen[key], duplicate_counts[key])
                duplicate_seen[key] += 1
            dot_points.append(draw_points[-1])
            arrow = _arrow_segment(draw_points)
            if arrow is not None:
                (x1, y1), (x2, y2) = arrow
                lines.append(f'<line class="arrow" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" marker-end="url(#arrow)" />')
    if cut_segments:
        sx, sy = screen(cut_segments[0][1][0])
        lines.append(f'<circle class="start" cx="{sx:.2f}" cy="{sy:.2f}" r="4" />')
    for point in dot_points:
        px, py = point
        lines.append(f'<circle class="dot" cx="{px:.2f}" cy="{py:.2f}" r="2.4" />')
    return lines


def _motion_styles_for_plan(plan: ToolpathPlan) -> list[str]:
    styles = [_motion_style("setup", move) for move in plan.commands if _is_motion(move)]
    for toolpath_pass in plan.passes:
        pass_style = "finish" if "finish" in toolpath_pass.kind else "cut"
        styles.extend(_motion_style(pass_style, move) for move in toolpath_pass.moves if _is_motion(move))
    return styles


def _motion_style(pass_style: str, move) -> str:
    if move.type == "rapid":
        return "rapid"
    return "finish" if pass_style == "finish" else "cut"


def _is_motion(move) -> bool:
    return move.type in {"rapid", "line", "arc"}


def _coincident_key(points: list[tuple[float, float]]) -> tuple[tuple[int, int], ...]:
    if len(points) <= 3:
        samples = points
    else:
        samples = [points[0], points[len(points) // 2], points[-1]]
    return tuple((round(x * 10), round(y * 10)) for x, y in samples)


def _fanout_points(points: list[tuple[float, float]], index: int, count: int) -> list[tuple[float, float]]:
    if count <= 1:
        return points
    vector = _dominant_vector(points)
    if vector is None:
        return points
    unit_x, unit_y = vector
    normal_x, normal_y = -unit_y, unit_x
    offset = (index - (count - 1) / 2) * 4.0
    return [(x + normal_x * offset, y + normal_y * offset) for x, y in points]


def _dominant_vector(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    best_length = 0.0
    for start, end in zip(points, points[1:], strict=False):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length > best_length:
            best = (dx / length, dy / length)
            best_length = length
    return best


def _arrow_segment(points: list[tuple[float, float]]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    lengths = [
        math.hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(points, points[1:], strict=False)
    ]
    total = sum(lengths)
    if total <= 1e-9:
        return None
    target = total / 2
    traveled = 0.0
    for start, end, length in zip(points, points[1:], lengths, strict=False):
        if length <= 1e-9:
            continue
        if traveled + length >= target:
            t = (target - traveled) / length
            center = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            unit = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
            half = min(7.0, max(4.0, length * 0.35))
            return (
                (center[0] - unit[0] * half, center[1] - unit[1] * half),
                (center[0] + unit[0] * half, center[1] + unit[1] * half),
            )
        traveled += length
    return None


def _move_points(move: CanonicalMove) -> list[tuple[float, float, float]]:
    if move.start is None or move.end is None:
        return []
    if move.kind != "arc" or move.center is None:
        return [move.start, move.end]
    start_angle = math.atan2(move.start[1] - move.center[1], move.start[0] - move.center[0])
    end_angle = math.atan2(move.end[1] - move.center[1], move.end[0] - move.center[0])
    if move.direction == "ccw" and end_angle <= start_angle:
        end_angle += math.tau
    if move.direction == "cw" and end_angle >= start_angle:
        end_angle -= math.tau
    steps = max(8, int(abs(end_angle - start_angle) / (math.pi / 16)))
    points = []
    radius = move.radius or math.hypot(move.start[0] - move.center[0], move.start[1] - move.center[1])
    for index in range(steps + 1):
        angle = start_angle + (end_angle - start_angle) * index / steps
        z = move.start[2] + (move.end[2] - move.start[2]) * index / steps
        points.append((move.center[0] + math.cos(angle) * radius, move.center[1] + math.sin(angle) * radius, z))
    return points


def _project(point: tuple[float, float, float], iso: bool) -> tuple[float, float]:
    x, y, z = point
    if not iso:
        return x, y
    return x - y * 0.48, (x + y) * 0.24 + z * 4.0


def _metrics(gcode: str) -> ReferenceMetrics:
    lifted = lift_gcode(gcode)
    lines = [line for line in gcode.splitlines() if line.strip()]
    motion = [move for move in lifted.moves if move.kind in {"rapid", "line", "arc"}]
    cut = [move for move in motion if move.kind in {"line", "arc"} and move.start is not None and move.end is not None]
    return ReferenceMetrics(
        lines=len(lines),
        moves=len(motion),
        rapid=sum(1 for move in motion if move.kind == "rapid"),
        line=sum(1 for move in motion if move.kind == "line"),
        arc=sum(1 for move in motion if move.kind == "arc"),
        cutting_length=round(sum(_xy_length(move) for move in cut), 4),
        g1=_count_motion_code(lines, "G1"),
        g2=_count_motion_code(lines, "G2"),
        g3=_count_motion_code(lines, "G3"),
    )


def _xy_spread(points: tuple[tuple[float, float, float, float], ...]) -> float:
    if not points:
        return 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _duplicate_cut_motion_count(gcode: str) -> int:
    seen: set[tuple] = set()
    duplicates = 0
    for move in lift_gcode(gcode).moves:
        if move.kind not in {"line", "arc"} or move.start is None or move.end is None:
            continue
        if move.kind == "line" and _same_xyz(move.start, move.end):
            continue
        if move.start[2] >= -1e-9 and move.end[2] >= -1e-9:
            continue
        key = _cut_motion_key(move)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _cut_motion_key(move: CanonicalMove) -> tuple:
    return (
        move.kind,
        _rounded_xyz(move.start),
        _rounded_xyz(move.end),
        None if move.center is None else _rounded_xy(move.center),
        move.direction,
    )


def _rounded_xyz(point: tuple[float, float, float] | None) -> tuple[int, int, int] | None:
    if point is None:
        return None
    return round(point[0] * 10_000), round(point[1] * 10_000), round(point[2] * 10_000)


def _rounded_xy(point: tuple[float, float]) -> tuple[int, int]:
    return round(point[0] * 10_000), round(point[1] * 10_000)


def _same_xyz(first: tuple[float, float, float], second: tuple[float, float, float]) -> bool:
    return (
        abs(first[0] - second[0]) <= 1e-9
        and abs(first[1] - second[1]) <= 1e-9
        and abs(first[2] - second[2]) <= 1e-9
    )


def _xy_length(move: CanonicalMove) -> float:
    if move.start is None or move.end is None:
        return 0.0
    if move.kind == "arc" and move.radius is not None and move.center is not None:
        start_angle = math.atan2(move.start[1] - move.center[1], move.start[0] - move.center[0])
        end_angle = math.atan2(move.end[1] - move.center[1], move.end[0] - move.center[0])
        if move.direction == "ccw" and end_angle <= start_angle:
            end_angle += math.tau
        if move.direction == "cw" and end_angle >= start_angle:
            end_angle -= math.tau
        return abs(end_angle - start_angle) * move.radius
    return math.hypot(move.end[0] - move.start[0], move.end[1] - move.start[1])


def _count_motion_code(lines: list[str], code: str) -> int:
    pattern = re.compile(rf"^{code}(?:\s|$)", re.IGNORECASE)
    return sum(1 for line in lines if pattern.match(line))


def _metrics_text(case: ReferenceCase, metrics: ReferenceMetrics) -> str:
    return (
        f"depth {case.depth:.4f}  tool {case.tool_diameter:.4f}  stepover {case.stepover_percent:.0f}%  "
        f"lines {metrics.lines}  moves {metrics.moves}  "
        f"G0/G1/G2/G3 {metrics.rapid}/{metrics.g1}/{metrics.g2}/{metrics.g3}  "
        f"cut length {metrics.cutting_length:.4f}"
    )


def _tool(diameter: float, depth_per_pass: float) -> Tool:
    return Tool.model_validate(
        {
            "id": "t1",
            "description": "reference flat endmill",
            "end_type": "flat",
            "flute_spiral": "upcut",
            "diameter": diameter,
            "flutes": 2,
            "speed": 18000,
            "feed_rate": 60,
            "plunge_rate": 20,
            "depth_per_pass": depth_per_pass,
        }
    )


def _pocket_operation(
    operation_id: str,
    entity: str,
    *,
    stepover_percent: float,
    depth: float = 0.125,
    lead_in: dict | None = None,
) -> PocketOperation:
    return PocketOperation.model_validate(
        {
            "id": operation_id,
            "type": "pocket",
            "entity": entity,
            "tool": "t1",
            "depth": depth,
            "strategy": "offset",
            "stepover_percent": stepover_percent,
            "roughing": {"enabled": True, "depth_per_pass": 0.125, "side_allowance": 0.0, "bottom_allowance": 0.0, "milling_direction": "climb"},
            "finishing": {"enabled": False},
            "lead_in": lead_in or {"type": "ramp", "length": 0.4},
        }
    )


def _helical_pocket_operation(
    operation_id: str,
    entity: str,
    *,
    depth: float = 0.125,
    pitch: float = 0.05,
    roughing_depth_per_pass: float = 0.125,
    roughing_side_allowance: float = 0.0,
    finishing: dict | None = None,
) -> HelicalPocketOperation:
    return HelicalPocketOperation.model_validate(
        {
            "id": operation_id,
            "type": "helical_pocket",
            "entity": entity,
            "tool": "t1",
            "depth": depth,
            "hole_diameter": 1.1,
            "pitch": pitch,
            "stepover_percent": 80,
            "prefer_arcs": True,
            "milling_direction": "climb",
            "roughing": {
                "enabled": True,
                "depth_per_pass": roughing_depth_per_pass,
                "side_allowance": roughing_side_allowance,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": finishing or {"enabled": False},
        }
    )


def _external_contour_operation(operation_id: str, entity: str) -> ContourOperation:
    return ContourOperation.model_validate(
        {
            "id": operation_id,
            "type": "contour",
            "entity": entity,
            "tool": "t1",
            "depth": 0.125,
            "extra_depth": 0.0,
            "offset": "outside",
            "ramping": True,
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.125,
                "side_allowance": 0.08,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
        }
    )


def _triangle_source_path(entity: str) -> SourcePath:
    return _path_from_points(entity, [(0.0, 0.42), (-0.46, -0.34), (0.46, -0.34)])


def _rectangle_source_path(entity: str, width: float, height: float) -> SourcePath:
    half_w = width / 2
    half_h = height / 2
    return _path_from_points(entity, [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)])


def _circle_source_path(entity: str, center: tuple[float, float], radius: float) -> SourcePath:
    cx, cy = center
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceArcSegment(type="arc", start=_point((cx + radius, cy)), end=_point((cx - radius, cy)), center=_point(center), radius=radius, direction="ccw"),
            SourceArcSegment(type="arc", start=_point((cx - radius, cy)), end=_point((cx + radius, cy)), center=_point(center), radius=radius, direction="ccw"),
        ],
    )


def _rounded_bulged_rectangle_source_path(entity: str) -> SourcePath:
    segments = [
        _line((-0.75, -0.45), (0.75, -0.45)),
        _arc((0.75, -0.20), 0.25, -90, 0),
        _line((1.00, -0.20), (1.00, -0.04)),
        _arc((1.00, 0.12), 0.16, -90, 90),
        _line((1.00, 0.28), (1.00, 0.45)),
        _arc((0.75, 0.45), 0.25, 0, 90),
        _line((0.75, 0.70), (-0.75, 0.70)),
        _arc((-0.75, 0.45), 0.25, 90, 180),
        _line((-1.00, 0.45), (-1.00, -0.20)),
        _arc((-0.75, -0.20), 0.25, 180, 270),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def _blobby_cross_source_path(entity: str) -> SourcePath:
    segments = [
        _arc((-0.72, 0.0), 0.42, 90, 270),
        _line((-0.72, -0.42), (-0.18, -0.42)),
        _line((-0.18, -0.42), (0.0, -0.64)),
        _line((0.0, -0.64), (0.18, -0.42)),
        _line((0.18, -0.42), (0.72, -0.42)),
        _arc((0.72, 0.0), 0.42, 270, 90),
        _line((0.72, 0.42), (0.18, 0.42)),
        _line((0.18, 0.42), (0.0, 0.64)),
        _line((0.0, 0.64), (-0.18, 0.42)),
        _line((-0.18, 0.42), (-0.72, 0.42)),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def _round_corner_protrusion_source_path(entity: str) -> SourcePath:
    lobe_radius = 0.5
    left_x = -1.3157894737
    right_x = 1.3157894737
    top_y = 0.7894736842
    bottom_y = -0.7894736842
    stem_half_width = 0.2947368421
    cap_center_y = 1.9894736842
    cap_radius = 0.2947368421
    segments = [
        _arc_cw((left_x, top_y), lobe_radius, 90, 0),
        _line((left_x + lobe_radius, top_y), (-stem_half_width, top_y)),
        _line((-stem_half_width, top_y), (-stem_half_width, cap_center_y)),
        _arc_cw((0.0, cap_center_y), cap_radius, 180, 0),
        _line((stem_half_width, cap_center_y), (stem_half_width, top_y)),
        _line((stem_half_width, top_y), (right_x - lobe_radius, top_y)),
        _arc_cw((right_x, top_y), lobe_radius, 180, 90),
        _arc_cw((right_x, top_y), lobe_radius, 90, -90),
        _line((right_x, top_y - lobe_radius), (right_x, bottom_y + lobe_radius)),
        _arc_cw((right_x, bottom_y), lobe_radius, 90, -90),
        _arc_cw((right_x, bottom_y), lobe_radius, -90, -180),
        _line((right_x - lobe_radius, bottom_y), (left_x + lobe_radius, bottom_y)),
        _arc_cw((left_x, bottom_y), lobe_radius, 0, -90),
        _arc_cw((left_x, bottom_y), lobe_radius, -90, -270),
        _line((left_x, bottom_y + lobe_radius), (left_x, top_y - lobe_radius)),
        _arc_cw((left_x, top_y), lobe_radius, -90, -270),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def _four_island_reference_case(operation: PocketOperation, tool: Tool) -> ReferenceCase:
    entity = "four-islands"
    outer = _rectangle_source_path(entity, 2.7, 1.8)
    islands = _four_island_source_paths()
    island_pocket = operation.model_copy(update={"id": "op-four-islands", "entity": entity})
    expected_geometry = _four_island_expected_geometry(outer, islands)
    return ReferenceCase(
        "four circular island pocket",
        outer,
        _island_pocket_toolpaths(island_pocket, outer, islands, tool, 0.5, expected_geometry),
        depth=island_pocket.depth,
        tool_diameter=tool.diameter,
        stepover_percent=island_pocket.stepover_percent,
        extra_source_paths=tuple(islands),
        expected_geometry=expected_geometry,
    )


def _four_island_source_paths() -> list[SourcePath]:
    centers = [(-0.65, -0.42), (0.65, -0.42), (0.65, 0.42), (-0.65, 0.42)]
    return [_circle_source_path(f"four-islands-island-{index}", center, 0.18) for index, center in enumerate(centers, start=1)]


def _four_island_expected_geometry(outer: SourcePath, islands: list[SourcePath]) -> Polygon:
    holes = [_source_polygon(island) for island in islands]
    polygon = Polygon(_source_polygon(outer), holes)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon


def _island_pocket_toolpaths(
    operation: PocketOperation,
    source_path: SourcePath,
    islands: list[SourcePath],
    tool: Tool,
    safe_z: float,
    expected_geometry: BaseGeometry,
) -> list[ToolpathPass]:
    cutter_radius = tool.diameter / 2
    feed = operation.feed_rate or tool.feed_rate
    stepover = tool.diameter * operation.stepover_percent / 100
    center_region = expected_geometry.buffer(-cutter_radius, join_style=1)
    depths = [operation.depth]
    moves = []
    for depth in depths:
        for segment in _island_raster_segments(center_region, stepover):
            moves.extend(_open_segment_moves(segment, -depth, safe_z, feed))
    return [
        ToolpathPass(
            id=f"{operation.id}-island-raster",
            operation_id=operation.id,
            entity=operation.entity,
            kind="pocket_clear",
            tool=operation.tool,
            tool_diameter=tool.diameter,
            feed_rate=feed,
            z_top=0.0,
            z_bottom=-operation.depth,
            source_path=source_path.id,
            offset_side="inside",
            offset_distance=cutter_radius,
            milling_direction=operation.roughing.milling_direction,
            moves=moves,
            warnings=[],
        )
    ]


def _island_raster_segments(geometry: BaseGeometry, stepover: float) -> list[list[tuple[float, float]]]:
    segments = []
    polygons = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    for polygon in polygons:
        if polygon.is_empty:
            continue
        for start, end in cavalier_pocketing._raster_segments(polygon, stepover):
            segments.append([start, end])
        for start, end in _vertical_raster_segments(polygon, stepover):
            segments.append([start, end])
    return segments


def _vertical_raster_segments(polygon: Polygon, stepover: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    min_x, min_y, max_x, max_y = polygon.bounds
    columns = []
    column_index = 0
    x = min_x + stepover / 2
    while x <= max_x - stepover / 2 + 1e-9:
        line = LineString([(x, min_y - stepover), (x, max_y + stepover)])
        intersections = cavalier_pocketing._lines(polygon.intersection(line))
        intersections.sort(key=lambda segment: segment.coords[0][1])
        if column_index % 2:
            intersections.reverse()
        for segment in intersections:
            coords = [(float(px), float(py)) for px, py in segment.coords]
            if len(coords) < 2 or segment.length <= 1e-9:
                continue
            start = coords[0]
            end = coords[-1]
            if column_index % 2:
                start, end = end, start
            columns.append((start, end))
        column_index += 1
        x += stepover
    return columns


def _open_segment_moves(points: list[tuple[float, float]], z_bottom: float, safe_z: float, feed: float) -> list:
    if len(points) < 2:
        return []
    start = points[0]
    return [
        RapidMove(type="rapid", x=start[0], y=start[1], z=safe_z),
        LineMove(type="line", z=z_bottom, feed=feed),
        *[LineMove(type="line", x=point[0], y=point[1], z=z_bottom, feed=feed) for point in points[1:]],
        RapidMove(type="rapid", z=safe_z),
    ]


def _capsule_notch_source_path(entity: str) -> SourcePath:
    segments = [
        _line((-1.0, -0.45), (0.75, -0.45)),
        _arc((0.75, 0.0), 0.45, -90, 90),
        _line((0.75, 0.45), (-0.55, 0.45)),
        _arc_cw((-0.55, 0.25), 0.2, 90, -90),
        _line((-0.55, 0.05), (-1.0, 0.05)),
        _line((-1.0, 0.05), (-1.0, -0.45)),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def _dogbone_spike_source_path(entity: str) -> SourcePath:
    segments = [
        _arc((0.75, 1.0), 0.75, 90, 270),
        _line((0.75, 0.25), (2.1, 0.25)),
        _line((2.1, 0.25), (2.55, -0.2)),
        _line((2.55, -0.2), (3.0, 0.25)),
        _line((3.0, 0.25), (4.25, 0.25)),
        _arc((4.25, 1.0), 0.75, 270, 90),
        _line((4.25, 1.75), (3.0, 1.75)),
        _line((3.0, 1.75), (2.55, 2.2)),
        _line((2.55, 2.2), (2.1, 1.75)),
        _line((2.1, 1.75), (0.75, 1.75)),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def _peninsula_source_path(entity: str) -> SourcePath:
    points = [
        (0.0, 0.0),
        (4.5, 0.0),
        (4.5, 2.2),
        (3.0, 2.2),
        (3.0, 1.1),
        (2.65, 1.1),
        (2.65, 2.2),
        (0.0, 2.2),
    ]
    return _path_from_points(entity, points)


def _path_from_points(entity: str, points: list[tuple[float, float]]) -> SourcePath:
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[_line(start, end) for start, end in zip(points, [*points[1:], points[0]], strict=True)],
    )


def _line(start: tuple[float, float], end: tuple[float, float]) -> SourceLineSegment:
    return SourceLineSegment(type="line", start=_point(start), end=_point(end))


def _arc(center: tuple[float, float], radius: float, start_angle: float, end_angle: float) -> SourceArcSegment:
    return SourceArcSegment(
        type="arc",
        start=_point(_arc_point(center, radius, start_angle)),
        end=_point(_arc_point(center, radius, end_angle)),
        center=_point(center),
        radius=radius,
        direction="ccw",
    )


def _arc_cw(center: tuple[float, float], radius: float, start_angle: float, end_angle: float) -> SourceArcSegment:
    return SourceArcSegment(
        type="arc",
        start=_point(_arc_point(center, radius, start_angle)),
        end=_point(_arc_point(center, radius, end_angle)),
        center=_point(center),
        radius=radius,
        direction="cw",
    )


def _arc_point(center: tuple[float, float], radius: float, angle: float) -> tuple[float, float]:
    radians = math.radians(angle)
    return center[0] + math.cos(radians) * radius, center[1] + math.sin(radians) * radius


def _point(coords: tuple[float, float]) -> Point2D:
    return Point2D(x=coords[0], y=coords[1])
