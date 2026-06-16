import math
from dataclasses import dataclass
from html import escape
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from dxfwiz.cam_kernel import cavalier
from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml
from dxfwiz.schemas import GeometryFile, PlannerFile
from dxfwiz.schemas.common import Point2D
from dxfwiz.schemas.job import ContourOperation, HelicalContourOperation, HelicalPocketOperation, PocketOperation
from dxfwiz.schemas.machine import Tool
from dxfwiz.toolpaths import pocketing_cavalier as cavalier_pocketing
from dxfwiz.toolpaths import operations as legacy_operations
from dxfwiz.toolpaths.core import GeometryResolver
from dxfwiz.toolpaths.drilling import helical_contour_operation_to_toolpaths, helical_pocket_operation_to_toolpaths
from dxfwiz.toolpaths.model import ArcMove, LineMove, RapidMove, SourceArcSegment, SourceLineSegment, SourcePath, ToolpathPass
from dxfwiz.toolpaths.operations import contour_operation_to_toolpaths
from dxfwiz.toolpaths.operations import _toolpath_render_segments, offset_source_path as shapely_offset_points, source_path_points
from dxfwiz.toolpaths.pocketing import pocket_operation_to_toolpaths
from dxfwiz.toolpaths.pocketing_cavalier import (
    boundary_raster_cavalier_toolpaths,
    contour_operation_to_cavalier_toolpaths,
    helical_contour_operation_to_cavalier_toolpaths,
    helical_pocket_operation_to_cavalier_toolpaths,
    pocket_operation_to_cavalier_toolpaths,
)
from dxfwiz.yaml_io import load_yaml_file

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
ROOT = Path(__file__).resolve().parents[2]
_TWO_X_INTAKE_PATHS: dict[str, SourcePath] | None = None


@dataclass(frozen=True)
class _EfficiencyMetrics:
    paths_per_z_level: int
    adjusted_ratio: float | None


def test_render_shapely_vs_cavalier_pocket_comparison_svg():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="offset", stepover_percent=80)
    raster_operation = make_pocket_operation(strategy="raster", stepover_percent=80)
    intake_paths = two_x_intake_source_paths()
    rows = [
        pocket_comparison_row("2xintake triangle cutout e3", intake_paths["e3"], operation, tool),
        pocket_comparison_row("2xintake triangle cutout e4", intake_paths["e4"], operation, tool),
        helical_pocket_comparison_row(
            "2xintake circular hole e10 / helical pocket",
            intake_paths["e10"],
            make_helical_pocket_operation("op-e10-helical-pocket", "e10"),
            tool,
        ),
        helical_pocket_comparison_row(
            "2xintake circular hole e24 / helical pocket",
            intake_paths["e24"],
            make_helical_pocket_operation("op-e24-helical-pocket", "e24"),
            tool,
        ),
        helical_pocket_comparison_row(
            "helical drill into helical pocket",
            circle_source_path("helical-drill-pocket", (0.0, 0.0), 0.45),
            make_helical_pocket_operation("op-helical-drill-pocket", "helical-drill-pocket", hole_diameter=0.9),
            tool,
        ),
        helical_contour_comparison_row(
            "2xintake circular hole e10 / helical contour",
            intake_paths["e10"],
            make_helical_contour_operation("op-e10-helical-contour", "e10"),
            tool,
        ),
        contour_comparison_row(
            "2xintake outer contour e2",
            intake_paths["e2"],
            make_contour_operation("op-e2-contour", "e2"),
            tool,
        ),
        contour_comparison_row(
            "contour vertical stepdowns / rectangle",
            square_source_path("vertical-contour", 4.0, 2.0),
            make_contour_operation("op-vertical-contour", "vertical-contour", ramping=False),
            tool,
        ),
        contour_comparison_row(
            "contour spiral ramp / rectangle",
            square_source_path("ramped-contour", 4.0, 2.0),
            make_contour_operation("op-ramped-contour", "ramped-contour", ramping=True),
            tool,
        ),
        contour_comparison_row(
            "tabbed contour vertical stepdowns",
            square_source_path("tabbed-cavc-contour", 4.0, 2.0),
            make_tabbed_contour_operation("op-tabbed-contour", "tabbed-cavc-contour", ramping=False),
            tool,
        ),
        contour_comparison_row(
            "tabbed contour ramp around tab",
            square_source_path("tabbed-cavc-ramp-contour", 4.0, 2.0),
            make_tabbed_contour_operation("op-tabbed-ramp-contour", "tabbed-cavc-ramp-contour", ramping=True),
            tool,
        ),
        pocket_comparison_row(
            "offset pocket taper entry / rounded side bulge",
            rounded_bulged_rectangle_source_path("pocket-ramp-entry"),
            make_pocket_operation(strategy="offset", stepover_percent=80, lead_in={"type": "ramp", "length": 0.5}),
            tool,
        ),
        pocket_comparison_row(
            "offset pocket vertical entry / rounded side bulge",
            rounded_bulged_rectangle_source_path("pocket-vertical-entry"),
            make_pocket_operation(strategy="offset", stepover_percent=80, lead_in={"type": "line", "length": 0.5}),
            tool,
        ),
    ]
    for label, source_path in [
        ("kiri-linked offsets / rounded rectangle with side bulge", rounded_bulged_rectangle_source_path("rounded-bulge")),
        ("kiri-linked offsets / four-lobe dogbone split pocket", four_lobe_dogbone_source_path("four-lobe-dogbone")),
        ("kiri-linked offsets / dogbone with middle spike", dogbone_spike_source_path("dogbone-spike")),
        ("kiri-linked offsets / long peninsula pocket", peninsula_source_path("peninsula")),
    ]:
        rows.append(pocket_comparison_row(label, source_path, operation, tool))
    rows.extend(
        [
            pocket_comparison_row(
                "boundary-linked raster / four-lobe dogbone",
                four_lobe_dogbone_source_path("boundary-raster-dogbone"),
                raster_operation,
                tool,
                cavalier_builder=boundary_raster_cavalier_toolpaths,
            ),
            pocket_comparison_row(
                "boundary-linked raster / rounded side bulge",
                rounded_bulged_rectangle_source_path("boundary-raster-bulge"),
                raster_operation,
                tool,
                cavalier_builder=boundary_raster_cavalier_toolpaths,
            ),
            pocket_comparison_row(
                "boundary-linked raster / peninsula",
                peninsula_source_path("boundary-raster-peninsula"),
                raster_operation,
                tool,
                cavalier_builder=boundary_raster_cavalier_toolpaths,
            ),
            offset_comparison_row("square inside/outside offsets", square_source_path("square", 2.0, 1.4), 0.15),
            offset_comparison_row("circle offsets preserve arcs", circle_source_path("circle", (1.0, 1.0), 0.8), 0.12),
            offset_comparison_row("concave L offsets", l_shape_source_path("concave-l"), 0.12),
            offset_comparison_row("arc-line capsule notch offsets", capsule_notch_source_path("capsule-notch"), 0.10),
            offset_comparison_row("rounded rectangle side bulge offsets", rounded_bulged_rectangle_source_path("offset-rounded-bulge"), 0.10),
        ]
    )

    svg = render_comparison_svg(rows, OUTPUT_DIR / "test_cavalier_pocket_comparison.svg")
    combined_svg = render_comparison_svg(rows, OUTPUT_DIR / "cavalier_vs_legacy_combined.svg")

    assert "four-lobe dogbone split pocket" in svg
    assert "2xintake triangle cutout e3" in svg
    assert "2xintake circular hole e10 / helical pocket" in svg
    assert "helical drill into helical pocket" in svg
    assert "2xintake outer contour e2" in svg
    assert "tabbed contour ramp around tab" in svg
    assert "offset pocket taper entry" in svg
    assert "paths/z " in svg
    assert "cut/ideal " in svg
    assert "boundary-linked raster / four-lobe dogbone" in svg
    assert "circle offsets preserve arcs" in svg
    assert "shapely top" in svg
    assert "cavalier top" in svg
    assert "2xintake outer contour e2" in combined_svg
    assert "boundary-linked raster / rounded side bulge" in combined_svg
    if cavalier.is_available():
        assert all(cavalier_passes for _label, _source, _shapely, cavalier_passes, _error, _ideal in rows)
    else:
        assert "native module is not installed" in svg


def test_comparison_iso_projects_cut_depth_below_reference_profile():
    top = _project((1.0, 1.0, 0.0), iso=True)
    cut = _project((1.0, 1.0, -0.25), iso=True)

    assert cut[1] < top[1]


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_non_tabbed_paths_do_not_use_legacy_arc_recovery(monkeypatch):
    def fail_arc_recovery(*_args, **_kwargs):
        raise AssertionError("Cavalier non-tabbed paths should preserve SourceArcSegment moves directly")

    monkeypatch.setattr(legacy_operations, "_recover_arc_segments", fail_arc_recovery)
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)

    contour_passes = contour_operation_to_cavalier_toolpaths(
        make_contour_operation("op-cavc-no-recovery-contour", "cavc-no-recovery-contour"),
        rounded_bulged_rectangle_source_path("cavc-no-recovery-contour"),
        tool,
        safe_z=0.5,
    )
    pocket_passes = pocket_operation_to_cavalier_toolpaths(
        make_pocket_operation(strategy="offset", stepover_percent=80),
        rounded_bulged_rectangle_source_path("cavc-no-recovery-pocket"),
        tool,
        safe_z=0.5,
    )

    assert any(move.type == "arc" for toolpath_pass in contour_passes for move in toolpath_pass.moves)
    assert any(move.type == "arc" for toolpath_pass in pocket_passes for move in toolpath_pass.moves)


def test_cavalier_source_path_bulge_round_trip_preserves_arcs():
    source_path = circle_source_path("round-trip", (0.0, 0.0), 1.0)
    converted = cavalier.bulge_vertices_to_source_path(
        source_path.id,
        source_path.entity,
        cavalier.source_path_to_bulge_vertices(source_path),
    )

    assert [segment.type for segment in converted.segments] == [segment.type for segment in source_path.segments]
    assert sum(1 for segment in converted.segments if segment.type == "arc") == sum(
        1 for segment in source_path.segments if segment.type == "arc"
    )


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offsets_closed_square_inside_and_outside():
    source_path = square_source_path("native-square", 1.0, 1.0)

    inside = cavalier.offset_source_path(source_path, "inside", 0.1)
    outside = cavalier.offset_source_path(source_path, "outside", 0.1)

    assert len(inside) == 1
    assert len(outside) == 1
    assert bbox(inside[0]) == pytest.approx((0.1, 0.1, 0.9, 0.9), abs=1e-6)
    assert bbox(outside[0]) == pytest.approx((-0.1, -0.1, 1.1, 1.1), abs=1e-6)
    assert any(segment.type == "arc" for segment in outside[0].segments)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offsets_circle_as_arc_segments():
    source_path = circle_source_path("native-circle", (0.0, 0.0), 1.0)

    inside = cavalier.offset_source_path(source_path, "inside", 0.125)
    outside = cavalier.offset_source_path(source_path, "outside", 0.125)

    assert len(inside) == 1
    assert len(outside) == 1
    assert all(segment.type == "arc" for segment in inside[0].segments)
    assert all(segment.type == "arc" for segment in outside[0].segments)
    assert [segment.radius for segment in inside[0].segments] == pytest.approx([0.875, 0.875], abs=1e-6)
    assert [segment.radius for segment in outside[0].segments] == pytest.approx([1.125, 1.125], abs=1e-6)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offsets_single_full_circle_source_arc():
    source_path = single_arc_circle_source_path("native-full-circle", (0.0, 0.0), 1.0)

    inside = cavalier.offset_source_path(source_path, "inside", 0.125)

    assert len(inside) == 1
    assert all(segment.type == "arc" for segment in inside[0].segments)
    assert [segment.radius for segment in inside[0].segments] == pytest.approx([0.875, 0.875], abs=1e-6)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_boolean_difference_exposes_island_region():
    outer = cavalier.source_path_to_bulge_vertices(square_source_path("outer", 4.0, 3.0))
    inner = cavalier.source_path_to_bulge_vertices(square_source_path("inner", 1.0, 1.0, origin=(1.5, 1.0)))

    result = cavalier.native().boolean_polylines(
        [cavalier.vertex_tuple(vertex) for vertex in outer],
        [cavalier.vertex_tuple(vertex) for vertex in inner],
        "difference",
    )

    assert len(result) >= 2


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offsets_preserve_arc_segments_on_dogbone_spike():
    offsets = cavalier.offset_source_path(four_lobe_dogbone_source_path("native-offset"), "inside", 0.125)

    assert offsets
    assert sum(1 for path in offsets for segment in path.segments if segment.type == "arc") >= 4


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_boundary_linked_raster_uses_bounded_entries_for_split_regions():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="raster", stepover_percent=80)

    passes = boundary_raster_cavalier_toolpaths(
        operation,
        four_lobe_dogbone_source_path("boundary-raster-test"),
        tool,
        safe_z=0.5,
    )

    assert len(passes) == 1
    rapid_xy_entries = [move for move in passes[0].moves if move.type == "rapid" and move.x is not None and move.y is not None]
    assert 1 <= len(rapid_xy_entries) <= 4
    assert sum(1 for move in passes[0].moves if move.type == "line" and move.x is not None and move.y is not None) > 10


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offset_pocket_links_loops_inside_travel_boundary():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    operation = make_pocket_operation(strategy="offset", stepover_percent=80)
    source_path = four_lobe_dogbone_source_path("linked-offset-test")

    passes = pocket_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)
    stepover = tool.diameter * operation.stepover_percent / 100
    levels = cavalier_pocketing._inward_offset_levels(source_path, tool.diameter / 2, stepover)
    unlinked_rapid_entries = sum(len(level) for level in levels) * 2
    linked_rapid_entries = [
        move for move in passes[0].moves if move.type == "rapid" and move.x is not None and move.y is not None
    ]

    assert len(linked_rapid_entries) < unlinked_rapid_entries
    assert len(linked_rapid_entries) <= 8


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_offset_pocket_ramp_entry_tapers_with_arc_moves():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.125)
    operation = make_pocket_operation(strategy="offset", stepover_percent=80, lead_in={"type": "ramp", "length": 0.5})

    passes = pocket_operation_to_cavalier_toolpaths(operation, circle_source_path("cavc-ramp-circle", (0.0, 0.0), 1.5), tool, safe_z=0.5)

    first_depth = -0.125
    taper_arcs = [
        move
        for move in passes[0].moves
        if move.type == "arc" and move.z is not None and first_depth < move.z < 0.0
    ]
    assert taper_arcs
    assert not any(move.type == "line" and move.x is None and move.y is None and move.z == pytest.approx(first_depth) for move in passes[0].moves[:4])


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_real_circle_helical_pocket_is_a_helix():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    source_path = two_x_intake_source_paths()["e24"]
    operation = make_helical_pocket_operation("op-e24-helical-pocket-test", "e24")

    passes = helical_pocket_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["helical_pocket", "finish_contour"]
    rough_arcs = [move for move in passes[0].moves if move.type == "arc"]
    assert len(rough_arcs) >= 8
    assert len({round(move.z, 4) for move in rough_arcs}) > 2
    assert passes[0].moves[-1].type == "arc"
    assert_arc_moves_are_geometrically_continuous(passes[0])


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_helical_drill_into_pocket_has_no_spiral_start_barb():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    source_path = circle_source_path("helical-drill-pocket-test", (0.0, 0.0), 0.45)
    operation = make_helical_pocket_operation(
        "op-helical-drill-pocket-test",
        "helical-drill-pocket-test",
        hole_diameter=0.9,
    )

    passes = helical_pocket_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)

    rough_arcs = [move for move in passes[0].moves if move.type == "arc"]
    assert len({round(move.z, 4) for move in rough_arcs}) > 2
    assert rough_arcs[-1].z == pytest.approx(-operation.depth)
    assert_arc_moves_are_geometrically_continuous(passes[0])
    metrics = path_efficiency_metrics(source_path, passes, _ideal_ratio(operation.stepover_percent))
    assert metrics.paths_per_z_level == 1
    assert metrics.adjusted_ratio == pytest.approx(1.29, abs=0.05)
    rendered_arc_segments = [
        points for kind, points in _toolpath_render_segments(passes[0])
        if kind == "arc" and abs(points[0][2] - points[-1][2]) > 1e-6
    ]
    assert rendered_arc_segments
    assert any(len({round(point[2], 4) for point in points}) > 2 for points in rendered_arc_segments)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_real_circle_helical_contour_uses_clean_helix():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    source_path = two_x_intake_source_paths()["e10"]
    operation = make_helical_contour_operation("op-e10-helical-contour-test", "e10")

    passes = helical_contour_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["helical_contour", "finish_contour"]
    rough_arcs = [move for move in passes[0].moves if move.type == "arc"]
    assert rough_arcs
    assert len({round(move.z, 4) for move in rough_arcs}) > 2
    assert all(move.direction == "ccw" for move in rough_arcs)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_contour_with_tabs_hops_over_tab_span():
    tool = make_test_tool(diameter=0.25, depth_per_pass=0.25)
    source_path = square_source_path("tabbed-cavc-contour-test", 4.0, 2.0)
    operation = make_tabbed_contour_operation("op-tabbed-contour-test", "tabbed-cavc-contour-test")

    passes = contour_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)

    z_values = [move.z for toolpath_pass in passes for move in toolpath_pass.moves if move.type == "line" and move.z is not None]
    assert any(z == pytest.approx(-0.15) for z in z_values)
    deep_segments = _xy_segments_at_z(passes[0], -0.25)
    assert not any(_segment_crosses_tab_span(segment, 1.38, 2.62) for segment in deep_segments)


@pytest.mark.skipif(not cavalier.is_available(), reason="dxfwiz_cavc native module is not installed")
def test_cavalier_inside_contour_warns_when_tool_does_not_fit():
    tool = make_test_tool(diameter=0.4, depth_per_pass=0.1)
    source_path = square_source_path("too-small-cavc-slot", 1.0, 0.39)
    operation = ContourOperation.model_validate(
        {
            "id": "op-cavc-inside-too-big",
            "type": "contour",
            "entity": "too-small-cavc-slot",
            "tool": "t5",
            "depth": 0.1,
            "offset": "inside",
            "roughing": {
                "enabled": True,
                "depth_per_pass": 0.1,
                "side_allowance": 0.0,
                "bottom_allowance": 0.0,
                "milling_direction": "climb",
            },
            "finishing": {
                "enabled": True,
                "side": True,
                "bottom": False,
                "passes": 1,
                "milling_direction": "climb",
            },
        }
    )

    passes = contour_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)

    assert [toolpath_pass.kind for toolpath_pass in passes] == ["rough_contour", "finish_contour"]
    assert all(toolpath_pass.moves == [] for toolpath_pass in passes)
    assert all("cannot be machined" in toolpath_pass.warnings[0] for toolpath_pass in passes)


def pocket_comparison_row(
    label: str,
    source_path: SourcePath,
    operation: PocketOperation,
    tool: Tool,
    *,
    cavalier_builder=pocket_operation_to_cavalier_toolpaths,
):
    shapely_passes = pocket_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)
    cavalier_passes = []
    cavalier_error = None
    if cavalier.is_available():
        cavalier_passes = cavalier_builder(operation, source_path, tool, safe_z=0.5)
    else:
        cavalier_error = "dxfwiz_cavc native module is not installed"
    return label, source_path, shapely_passes, cavalier_passes, cavalier_error, _ideal_ratio(operation.stepover_percent)


def contour_comparison_row(label: str, source_path: SourcePath, operation: ContourOperation, tool: Tool):
    shapely_passes = contour_operation_to_toolpaths(operation, source_path, tool, safe_z=0.5)
    cavalier_passes = []
    cavalier_error = None
    if cavalier.is_available():
        cavalier_passes = contour_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)
    else:
        cavalier_error = "native module is not installed"
    return label, source_path, shapely_passes, cavalier_passes, cavalier_error, None


def helical_pocket_comparison_row(label: str, source_path: SourcePath, operation: HelicalPocketOperation, tool: Tool):
    center, hole_diameter = circle_center_and_diameter(source_path)
    shapely_passes = helical_pocket_operation_to_toolpaths(operation, center, hole_diameter, tool, safe_z=0.5)
    cavalier_passes = []
    cavalier_error = None
    if cavalier.is_available():
        cavalier_passes = helical_pocket_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)
    else:
        cavalier_error = "native module is not installed"
    return label, source_path, shapely_passes, cavalier_passes, cavalier_error, _ideal_ratio(operation.stepover_percent)


def helical_contour_comparison_row(label: str, source_path: SourcePath, operation: HelicalContourOperation, tool: Tool):
    center, hole_diameter = circle_center_and_diameter(source_path)
    shapely_passes = helical_contour_operation_to_toolpaths(operation, center, hole_diameter, tool, safe_z=0.5)
    cavalier_passes = []
    cavalier_error = None
    if cavalier.is_available():
        cavalier_passes = helical_contour_operation_to_cavalier_toolpaths(operation, source_path, tool, safe_z=0.5)
    else:
        cavalier_error = "native module is not installed"
    return label, source_path, shapely_passes, cavalier_passes, cavalier_error, None


def offset_comparison_row(label: str, source_path: SourcePath, distance: float):
    shapely_paths = [
        path_from_points(f"{source_path.entity}-shapely-inside", shapely_offset_points(source_path, "inside", distance)),
        path_from_points(f"{source_path.entity}-shapely-outside", shapely_offset_points(source_path, "outside", distance)),
    ]
    shapely_passes = [
        source_paths_to_pass(f"{source_path.entity}-shapely-offsets", source_path.entity, shapely_paths, "pocket_floor_finish")
    ]
    cavalier_passes = []
    cavalier_error = None
    if cavalier.is_available():
        cavalier_paths = [
            *cavalier.offset_source_path(source_path, "inside", distance),
            *cavalier.offset_source_path(source_path, "outside", distance),
        ]
        cavalier_passes = [
            source_paths_to_pass(f"{source_path.entity}-cavalier-offsets", source_path.entity, cavalier_paths, "pocket_floor_finish")
        ]
    else:
        cavalier_error = "dxfwiz_cavc native module is not installed"
    return label, source_path, shapely_passes, cavalier_passes, cavalier_error, None


def _ideal_ratio(stepover_percent: float) -> float:
    return 100.0 / stepover_percent


def two_x_intake_source_paths() -> dict[str, SourcePath]:
    global _TWO_X_INTAKE_PATHS
    if _TWO_X_INTAKE_PATHS is not None:
        return _TWO_X_INTAKE_PATHS

    input_dir = ROOT / "tests" / "integration_tests" / "2xintake"
    source_dxf = input_dir / "2xintakev3_and_2xkickerv1.dxf"
    planner = PlannerFile.model_validate(load_yaml_file(input_dir / "planner.yaml"))
    output_dir = ROOT / "tests" / "output" / "2xintake"
    output_dir.mkdir(parents=True, exist_ok=True)
    fixed_dxf = output_dir / "2xintakev3_and_2xkickerv1_fixed.dxf"
    geom_yaml = output_dir / "2xintakev3_and_2xkickerv1_geom.yaml"
    clean_dxf(source_dxf, fixed_dxf, _clean_config(planner))
    write_geometry_yaml(
        fixed_dxf,
        geom_yaml,
        original_file=source_dxf.name,
        cleaned_file=fixed_dxf.name,
    )
    geometry = GeometryFile.model_validate(load_yaml_file(geom_yaml))
    resolver = GeometryResolver(geometry=geometry, generated={}, fixed_dxf=fixed_dxf)
    entity_ids = ["e2", "e3", "e4", "e10", "e24"]
    _TWO_X_INTAKE_PATHS = {entity_id: resolver.source_path(entity_id) for entity_id in entity_ids}
    assert all(_TWO_X_INTAKE_PATHS.values())
    return _TWO_X_INTAKE_PATHS


def _clean_config(planner: PlannerFile) -> CleanDxfConfig:
    arc_detection = planner.defaults.arc_detection
    return CleanDxfConfig(
        gap_tolerance=0.005,
        duplicate_tolerance=0.0005,
        min_segment_length=0.001,
        arc_detection=arc_detection.mode if arc_detection else "OFF",
        arc_tolerance=arc_detection.tolerance if arc_detection else 0.002,
        reorient_to_origin=planner.defaults.origin.reorient_to_origin,
    )


def circle_center_and_diameter(source_path: SourcePath) -> tuple[tuple[float, float], float]:
    arc_segment = next(segment for segment in source_path.segments if isinstance(segment, SourceArcSegment))
    return (arc_segment.center.x, arc_segment.center.y), arc_segment.radius * 2


def make_contour_operation(operation_id: str, entity: str, ramping: bool = True) -> ContourOperation:
    return ContourOperation.model_validate(
        {
            "id": operation_id,
            "type": "contour",
            "entity": entity,
            "tool": "t5",
            "depth": 0.25,
            "extra_depth": 0.01,
            "offset": "outside",
            "ramping": ramping,
            "roughing": {"enabled": True, "depth_per_pass": 0.125, "side_allowance": 0.01, "milling_direction": "climb"},
            "finishing": {"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
        }
    )


def make_tabbed_contour_operation(operation_id: str, entity: str, ramping: bool = False) -> ContourOperation:
    return ContourOperation.model_validate(
        {
            "id": operation_id,
            "type": "contour",
            "entity": entity,
            "tool": "t5",
            "depth": 0.25,
            "offset": "on",
            "ramping": ramping,
            "roughing": {"enabled": True, "depth_per_pass": 0.25, "side_allowance": 0.0, "bottom_allowance": 0.0, "milling_direction": "climb"},
            "finishing": {"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
            "tabs": tabs_on_bottom_edge(height=0.1),
        }
    )


def tabs_on_bottom_edge(height: float = 0.1):
    return {
        "enabled": True,
        "width": 1.0,
        "height": height,
        "count": 1,
        "locations": [
            {
                "center": {"x": 2.0, "y": 0.0},
                "lower_left": {"x": 1.5, "y": -0.1},
                "upper_right": {"x": 2.5, "y": 0.1},
                "width": 1.0,
                "height": height,
                "angle_deg": 0.0,
            }
        ],
    }


def make_helical_pocket_operation(operation_id: str, entity: str, hole_diameter: float = 1.125) -> HelicalPocketOperation:
    return HelicalPocketOperation.model_validate(
        {
            "id": operation_id,
            "type": "helical_pocket",
            "entity": entity,
            "tool": "t5",
            "depth": 0.25,
            "hole_diameter": hole_diameter,
            "pitch": 0.08,
            "stepover_percent": 40,
            "prefer_arcs": True,
            "milling_direction": "climb",
            "roughing": {"enabled": True, "depth_per_pass": 0.08, "side_allowance": 0.01, "bottom_allowance": 0.0, "milling_direction": "climb"},
            "finishing": {"enabled": True, "side": True, "bottom": True, "passes": 1, "milling_direction": "climb"},
        }
    )


def make_helical_contour_operation(operation_id: str, entity: str) -> HelicalContourOperation:
    return HelicalContourOperation.model_validate(
        {
            "id": operation_id,
            "type": "helical_contour",
            "entity": entity,
            "tool": "t5",
            "depth": 0.25,
            "pitch": 0.08,
            "milling_direction": "climb",
            "finishing": {"enabled": True, "side": True, "bottom": False, "passes": 1, "milling_direction": "climb"},
        }
    )


def render_comparison_svg(rows, output_path: Path) -> str:
    width = 1800
    row_height = 430
    label_width = 260
    cell_width = (width - label_width) / 4
    height = max(row_height * len(rows), row_height)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text { font-family: Arial, sans-serif; font-size: 14px; fill: #111827; }",
        ".title { font-size: 19px; font-weight: 700; }",
        ".label { font-weight: 700; }",
        ".meta { fill: #64748b; font-size: 12px; }",
        ".source { fill: none; stroke: #111827; stroke-width: 2.5; }",
        ".clear { fill: none; stroke: #7c3aed; stroke-width: 2; }",
        ".finish { fill: none; stroke: #dc2626; stroke-width: 2; }",
        ".floor { fill: none; stroke: #f59e0b; stroke-width: 2; }",
        ".rapid { fill: none; stroke: #94a3b8; stroke-width: 1.4; stroke-dasharray: 5 5; }",
        ".arc { stroke-width: 3.2; }",
        ".divider { stroke: #d1d5db; stroke-width: 1; }",
        "</style>",
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff" />',
    ]
    headers = ["shapely top", "shapely iso", "cavalier top", "cavalier iso"]
    for row_index, (label, source_path, shapely_passes, cav_passes, cav_error, ideal_ratio) in enumerate(rows):
        y = row_index * row_height
        if row_index:
            lines.append(f'<line class="divider" x1="0" y1="{y}" x2="{width}" y2="{y}" />')
        lines.append(f'<text class="title" x="24" y="{y + 34}">{escape(label)}</text>')
        lines.append(f'<text class="meta" x="24" y="{y + 58}">model extents: {_extents_text(source_path)}</text>')
        for column, header in enumerate(headers):
            x = label_width + column * cell_width
            lines.append(f'<text class="label" x="{x + 16:.1f}" y="{y + 34}">{header}</text>')
        lines.extend(_comparison_cell(source_path, shapely_passes, ideal_ratio, label_width, y + 58, cell_width, row_height - 76, iso=False))
        lines.extend(_comparison_cell(source_path, shapely_passes, ideal_ratio, label_width + cell_width, y + 58, cell_width, row_height - 76, iso=True))
        if cav_error:
            lines.append(f'<text class="meta" x="{label_width + 2 * cell_width + 20:.1f}" y="{y + 120}">{escape(cav_error)}</text>')
            lines.append(f'<text class="meta" x="{label_width + 3 * cell_width + 20:.1f}" y="{y + 120}">{escape(cav_error)}</text>')
        else:
            lines.extend(_comparison_cell(source_path, cav_passes, ideal_ratio, label_width + 2 * cell_width, y + 58, cell_width, row_height - 76, iso=False))
            lines.extend(_comparison_cell(source_path, cav_passes, ideal_ratio, label_width + 3 * cell_width, y + 58, cell_width, row_height - 76, iso=True))
    lines.append("</svg>")
    svg = "\n".join(lines) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    return svg


def _comparison_cell(
    source_path: SourcePath,
    passes,
    ideal_ratio: float | None,
    x: float,
    y: float,
    width: float,
    height: float,
    iso: bool,
) -> list[str]:
    render_sets = []
    source_points = [(x, y, 0.0) for x, y in source_path_points(source_path)]
    render_sets.append(("source", source_points))
    for toolpath_pass in passes:
        for kind, points in _toolpath_render_segments(toolpath_pass):
            css = "rapid" if kind == "rapid" else _css_for_kind(toolpath_pass.kind)
            if kind == "arc":
                css += " arc"
            render_sets.append((css, points))
    metrics = path_efficiency_metrics(source_path, passes, ideal_ratio)
    all_points = [_project(point, iso) for _css, points in render_sets for point in points]
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    metrics_height = 22
    scale = min((width - 32) / max(max_x - min_x, 1e-9), (height - 32 - metrics_height) / max(max_y - min_y, 1e-9))
    lines = [f'<text class="meta" x="{x + 16:.1f}" y="{y + 16:.1f}">{escape(_metrics_text(metrics))}</text>']
    for css, points in render_sets:
        screen_points = []
        for point in points:
            px, py = _project(point, iso)
            sx = x + 16 + (px - min_x) * scale
            sy = y + 16 + metrics_height + (max_y - py) * scale
            screen_points.append(f"{sx:.2f},{sy:.2f}")
        close = "source" in css
        if close and screen_points:
            screen_points.append(screen_points[0])
        lines.append(f'<polyline class="{css}" points="{" ".join(screen_points)}" />')
    return lines


def path_efficiency_metrics(source_path: SourcePath, passes, ideal_ratio: float | None = None) -> _EfficiencyMetrics:
    measured_passes = _metric_passes(passes)
    tool_diameter = 0.0
    paths_by_z: dict[float, list[list[tuple[float, float, float]]]] = {}
    for toolpath_pass in measured_passes:
        tool_diameter = toolpath_pass.tool_diameter or tool_diameter
        for path_points in _constant_z_cut_paths(toolpath_pass):
            if len(path_points) < 2:
                continue
            z = round(path_points[0][2], 6)
            paths_by_z.setdefault(z, []).append(path_points)
    measured_z = min(paths_by_z) if paths_by_z else None
    measured_paths = paths_by_z.get(measured_z, []) if measured_z is not None else []
    path_length = sum(_xy_polyline_length(points) for points in measured_paths)
    area = _metric_area(source_path, measured_passes, tool_diameter, measured_paths)
    removal_ratio = None if area <= 1e-9 or tool_diameter <= 0 else path_length * tool_diameter / area
    adjusted_ratio = None if removal_ratio is None or ideal_ratio is None else removal_ratio / ideal_ratio
    return _EfficiencyMetrics(paths_per_z_level=len(measured_paths), adjusted_ratio=adjusted_ratio)


def _metric_passes(passes) -> list[ToolpathPass]:
    clearing_kinds = {"pocket_clear", "helical_pocket"}
    clearing_passes = [toolpath_pass for toolpath_pass in passes if toolpath_pass.kind in clearing_kinds]
    return clearing_passes or list(passes)


def _constant_z_cut_paths(toolpath_pass: ToolpathPass) -> list[list[tuple[float, float, float]]]:
    paths = []
    current_path: list[tuple[float, float, float]] = []
    for kind, points in _toolpath_render_segments(toolpath_pass):
        if kind == "rapid":
            if current_path:
                paths.append(current_path)
                current_path = []
            continue
        for start, end in zip(points, points[1:], strict=False):
            if abs(start[2] - end[2]) > 1e-6:
                if current_path:
                    paths.append(current_path)
                    current_path = []
                continue
            if not current_path:
                current_path = [start, end]
            elif _same_xyz(current_path[-1], start):
                current_path.append(end)
            else:
                paths.append(current_path)
                current_path = [start, end]
    if current_path:
        paths.append(current_path)
    if toolpath_pass.kind == "helical_pocket":
        paths = _drop_terminal_cleanup_loop(paths)
    return paths


def _drop_terminal_cleanup_loop(paths: list[list[tuple[float, float, float]]]) -> list[list[tuple[float, float, float]]]:
    if not paths:
        return paths
    last = paths[-1]
    if len(last) > 3 and _same_xy(last[0], last[-1]):
        return paths[:-1]
    trimmed = _trim_terminal_closed_loop(last)
    if trimmed is not last:
        return [*paths[:-1], trimmed] if len(trimmed) >= 2 else paths[:-1]
    return paths


def _trim_terminal_closed_loop(path: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    if len(path) < 4:
        return path
    end = path[-1]
    for index in range(len(path) - 3, -1, -1):
        if _same_xy(path[index], end):
            return path[: index + 1]
    return path


def _same_xy(first: tuple[float, float, float], second: tuple[float, float, float]) -> bool:
    return abs(first[0] - second[0]) <= 1e-6 and abs(first[1] - second[1]) <= 1e-6


def _same_xyz(first: tuple[float, float, float], second: tuple[float, float, float]) -> bool:
    return (
        abs(first[0] - second[0]) <= 1e-9
        and abs(first[1] - second[1]) <= 1e-9
        and abs(first[2] - second[2]) <= 1e-9
    )


def _xy_segments_at_z(toolpath_pass: ToolpathPass, z: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments = []
    for kind, points in _toolpath_render_segments(toolpath_pass):
        if kind == "rapid":
            continue
        for start, end in zip(points, points[1:], strict=False):
            if start[2] == pytest.approx(z) and end[2] == pytest.approx(z):
                segments.append(((start[0], start[1]), (end[0], end[1])))
    return segments


def _segment_crosses_tab_span(segment: tuple[tuple[float, float], tuple[float, float]], min_x: float, max_x: float) -> bool:
    start, end = segment
    if abs(start[1]) > 1e-6 or abs(end[1]) > 1e-6:
        return False
    return min(start[0], end[0]) <= max_x and max(start[0], end[0]) >= min_x


def assert_arc_moves_are_geometrically_continuous(toolpath_pass: ToolpathPass) -> None:
    current_x = None
    current_y = None
    current_z = None
    for move in toolpath_pass.moves:
        if move.type == "rapid":
            current_x = move.x if move.x is not None else current_x
            current_y = move.y if move.y is not None else current_y
            current_z = move.z if move.z is not None else current_z
            continue
        if move.type == "line":
            current_x = move.x if move.x is not None else current_x
            current_y = move.y if move.y is not None else current_y
            current_z = move.z if move.z is not None else current_z
            continue
        if move.type != "arc":
            continue
        assert current_x is not None
        assert current_y is not None
        center_x = current_x + move.i
        center_y = current_y + move.j
        start_radius = math.hypot(current_x - center_x, current_y - center_y)
        end_radius = math.hypot(move.x - center_x, move.y - center_y)
        assert end_radius == pytest.approx(start_radius, abs=1e-6)
        current_x = move.x
        current_y = move.y
        current_z = move.z if move.z is not None else current_z


def _metrics_text(metrics: _EfficiencyMetrics) -> str:
    adjusted = "n/a" if metrics.adjusted_ratio is None else f"{metrics.adjusted_ratio:.2f}x"
    return f"paths/z {metrics.paths_per_z_level}  cut/ideal {adjusted}"


def _xy_polyline_length(points: list[tuple[float, float, float]]) -> float:
    return sum(
        math.hypot(second[0] - first[0], second[1] - first[1])
        for first, second in zip(points, points[1:], strict=False)
    )


def _metric_area(
    source_path: SourcePath,
    passes,
    tool_diameter: float,
    measured_paths: list[list[tuple[float, float, float]]] | None = None,
) -> float:
    if measured_paths and any(toolpath_pass.kind == "helical_pocket" for toolpath_pass in passes):
        annulus_area = _helical_pocket_measured_annulus_area(source_path, measured_paths)
        if annulus_area > 1e-9:
            return annulus_area
    points = source_path_points(source_path, arc_segments=96)
    if len(points) < 3:
        return 0.0
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return 0.0
    pocket_like = any(
        toolpath_pass.kind in {"pocket_clear", "pocket_floor_finish", "pocket_wall_finish", "helical_pocket"}
        or toolpath_pass.offset_side == "inside"
        for toolpath_pass in passes
    )
    if pocket_like and tool_diameter > 0:
        offset = polygon.buffer(-tool_diameter / 2, join_style="round")
        if not offset.is_empty and offset.area > 1e-9:
            return float(offset.area)
    return float(polygon.area)


def _helical_pocket_measured_annulus_area(
    source_path: SourcePath,
    measured_paths: list[list[tuple[float, float, float]]],
) -> float:
    center = _source_path_circle_center(source_path)
    if center is None:
        return 0.0
    radii = [
        math.hypot(point[0] - center[0], point[1] - center[1])
        for path in measured_paths
        for point in path
    ]
    if not radii:
        return 0.0
    inner = min(radii)
    outer = max(radii)
    if outer <= inner + 1e-9:
        return 0.0
    return math.pi * (outer * outer - inner * inner)


def _source_path_circle_center(source_path: SourcePath) -> tuple[float, float] | None:
    arc_segments = [segment for segment in source_path.segments if isinstance(segment, SourceArcSegment)]
    if not arc_segments:
        return None
    return (
        sum(segment.center.x for segment in arc_segments) / len(arc_segments),
        sum(segment.center.y for segment in arc_segments) / len(arc_segments),
    )


def _project(point: tuple[float, float, float], iso: bool) -> tuple[float, float]:
    x, y, z = point
    if not iso:
        return x, y
    return x - y * 0.48, (x + y) * 0.24 + z * 5.0


def _css_for_kind(kind: str) -> str:
    if kind == "pocket_wall_finish":
        return "finish"
    if kind == "pocket_floor_finish":
        return "floor"
    return "clear"


def _extents_text(source_path: SourcePath) -> str:
    points = source_path_points(source_path)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return f"{max(xs) - min(xs):.3f} x {max(ys) - min(ys):.3f}"


def make_pocket_operation(**overrides) -> PocketOperation:
    data = {
        "id": "op-pocket",
        "type": "pocket",
        "entity": "pocket-entity",
        "tool": "t5",
        "depth": 0.25,
        "strategy": "offset",
        "stepover_percent": 40,
        "roughing": {"enabled": True, "depth_per_pass": 0.125, "side_allowance": 0.0, "bottom_allowance": 0.0, "milling_direction": "climb"},
        "finishing": {"enabled": True, "side": True, "bottom": True, "passes": 1, "milling_direction": "climb"},
        "lead_in": {"type": "ramp", "length": 0.5},
    }
    data.update(overrides)
    return PocketOperation.model_validate(data)


def make_test_tool(diameter: float, depth_per_pass: float) -> Tool:
    return Tool.model_validate(
        {
            "id": "t5",
            "description": "test tool",
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


def square_source_path(
    entity: str,
    width: float,
    height: float,
    *,
    origin: tuple[float, float] = (0.0, 0.0),
) -> SourcePath:
    x, y = origin
    return path_from_points(
        entity,
        [
            (x, y),
            (x + width, y),
            (x + width, y + height),
            (x, y + height),
        ],
    )


def l_shape_source_path(entity: str) -> SourcePath:
    return path_from_points(
        entity,
        [
            (0.0, 0.0),
            (2.4, 0.0),
            (2.4, 0.8),
            (1.05, 0.8),
            (1.05, 2.1),
            (0.0, 2.1),
        ],
    )


def circle_source_path(entity: str, center: tuple[float, float], radius: float) -> SourcePath:
    cx, cy = center
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceArcSegment(
                type="arc",
                start=point((cx + radius, cy)),
                end=point((cx - radius, cy)),
                center=point(center),
                radius=radius,
                direction="ccw",
            ),
            SourceArcSegment(
                type="arc",
                start=point((cx - radius, cy)),
                end=point((cx + radius, cy)),
                center=point(center),
                radius=radius,
                direction="ccw",
            ),
        ],
    )


def single_arc_circle_source_path(entity: str, center: tuple[float, float], radius: float) -> SourcePath:
    cx, cy = center
    start = point((cx + radius, cy))
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[
            SourceArcSegment(
                type="arc",
                start=start,
                end=start,
                center=point(center),
                radius=radius,
                direction="ccw",
            )
        ],
    )


def capsule_notch_source_path(entity: str) -> SourcePath:
    segments = [
        line((-1.0, -0.45), (0.75, -0.45)),
        arc((0.75, 0.0), 0.45, -90, 90),
        line((0.75, 0.45), (-0.55, 0.45)),
        arc_cw((-0.55, 0.25), 0.2, 90, -90),
        line((-0.55, 0.05), (-1.0, 0.05)),
        line((-1.0, 0.05), (-1.0, -0.45)),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def four_lobe_dogbone_source_path(entity: str) -> SourcePath:
    lobes = [
        Point(0.0, 0.0).buffer(0.55, quad_segs=10),
        Point(2.0, 0.0).buffer(0.55, quad_segs=10),
        Point(0.0, 1.6).buffer(0.55, quad_segs=10),
        Point(2.0, 1.6).buffer(0.55, quad_segs=10),
    ]
    bridges = [
        box(-0.05, -0.20, 2.05, 0.20),
        box(-0.05, 1.40, 2.05, 1.80),
        box(0.82, -0.05, 1.18, 1.65),
    ]
    polygon = unary_union([*lobes, *bridges]).buffer(0)
    return source_path_from_polygon(entity, polygon)


def rounded_bulged_rectangle_source_path(entity: str) -> SourcePath:
    segments = [
        line((0.3, 0.0), (3.7, 0.0)),
        arc((3.7, 0.3), 0.3, -90, 0),
        line((4.0, 0.3), (4.0, 0.8)),
        arc((4.0, 1.0), 0.2, -90, 90),
        line((4.0, 1.2), (4.0, 1.7)),
        arc((3.7, 1.7), 0.3, 0, 90),
        line((3.7, 2.0), (0.3, 2.0)),
        arc((0.3, 1.7), 0.3, 90, 180),
        line((0.0, 1.7), (0.0, 0.3)),
        arc((0.3, 0.3), 0.3, 180, 270),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def dogbone_spike_source_path(entity: str) -> SourcePath:
    segments = [
        arc((0.75, 1.0), 0.75, 90, 270),
        line((0.75, 0.25), (2.1, 0.25)),
        line((2.1, 0.25), (2.55, -0.2)),
        line((2.55, -0.2), (3.0, 0.25)),
        line((3.0, 0.25), (4.25, 0.25)),
        arc((4.25, 1.0), 0.75, 270, 90),
        line((4.25, 1.75), (3.0, 1.75)),
        line((3.0, 1.75), (2.55, 2.2)),
        line((2.55, 2.2), (2.1, 1.75)),
        line((2.1, 1.75), (0.75, 1.75)),
    ]
    return SourcePath(id=f"path-{entity}", entity=entity, closed=True, segments=segments)


def peninsula_source_path(entity: str) -> SourcePath:
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
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[line(start, end) for start, end in zip(points, [*points[1:], points[0]], strict=True)],
    )


def path_from_points(entity: str, points: list[tuple[float, float]]) -> SourcePath:
    return SourcePath(
        id=f"path-{entity}",
        entity=entity,
        closed=True,
        segments=[line(start, end) for start, end in zip(points, [*points[1:], points[0]], strict=True)],
    )


def source_path_from_polygon(entity: str, polygon) -> SourcePath:
    coords = [(float(x), float(y)) for x, y in polygon.exterior.coords[:-1]]
    if signed_area(coords) < 0:
        coords.reverse()
    return path_from_points(entity, coords)


def source_paths_to_pass(pass_id: str, entity: str, paths: list[SourcePath], kind: str) -> ToolpathPass:
    moves = []
    for source_path in paths:
        moves.extend(closed_source_path_moves(source_path, -0.1, 0.5, 60))
    return ToolpathPass(
        id=pass_id,
        operation_id=pass_id,
        entity=entity,
        kind=kind,
        tool="t5",
        tool_diameter=0.25,
        feed_rate=60,
        z_top=0.0,
        z_bottom=-0.1,
        source_path=entity,
        offset_side="inside",
        offset_distance=0.1,
        milling_direction="climb",
        moves=moves,
    )


def closed_source_path_moves(source_path: SourcePath, z_bottom: float, safe_z: float, feed: float):
    if not source_path.segments:
        return []
    start = source_path.segments[0].start
    moves = [RapidMove(type="rapid", x=start.x, y=start.y, z=safe_z), LineMove(type="line", z=z_bottom, feed=feed)]
    for segment in source_path.segments:
        if isinstance(segment, SourceLineSegment):
            moves.append(LineMove(type="line", x=segment.end.x, y=segment.end.y, z=z_bottom, feed=feed))
        else:
            moves.append(
                ArcMove(
                    type="arc",
                    direction=segment.direction,
                    x=segment.end.x,
                    y=segment.end.y,
                    z=z_bottom,
                    i=segment.center.x - segment.start.x,
                    j=segment.center.y - segment.start.y,
                    feed=feed,
                )
            )
    moves.append(RapidMove(type="rapid", z=safe_z))
    return moves


def line(start: tuple[float, float], end: tuple[float, float]) -> SourceLineSegment:
    return SourceLineSegment(type="line", start=point(start), end=point(end))


def arc(center: tuple[float, float], radius: float, start_angle: float, end_angle: float) -> SourceArcSegment:
    start = _arc_point(center, radius, start_angle)
    end = _arc_point(center, radius, end_angle)
    return SourceArcSegment(
        type="arc",
        start=point(start),
        end=point(end),
        center=point(center),
        radius=radius,
        direction="ccw",
    )


def arc_cw(center: tuple[float, float], radius: float, start_angle: float, end_angle: float) -> SourceArcSegment:
    start = _arc_point(center, radius, start_angle)
    end = _arc_point(center, radius, end_angle)
    return SourceArcSegment(
        type="arc",
        start=point(start),
        end=point(end),
        center=point(center),
        radius=radius,
        direction="cw",
    )


def point(coords: tuple[float, float]) -> Point2D:
    return Point2D(x=coords[0], y=coords[1])


def _arc_point(center: tuple[float, float], radius: float, angle: float) -> tuple[float, float]:
    radians = math.radians(angle)
    return center[0] + math.cos(radians) * radius, center[1] + math.sin(radians) * radius


def signed_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, [*points[1:], points[0]], strict=False)
    )


def bbox(source_path: SourcePath) -> tuple[float, float, float, float]:
    points = source_path_points(source_path, arc_segments=96)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)
