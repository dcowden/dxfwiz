from __future__ import annotations

from html import escape
from io import StringIO
from pathlib import Path

import ezdxf
from shapely.geometry import Point

from dxfwiz.planning.service import PlanningRequest, _part_clearance_polygons, _screw_points
from dxfwiz.schemas import GeometryFile, MachineFile


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
PREVIEW_FILE = OUTPUT_DIR / "screw_placement.svg"
PREVIEW_SECTIONS: dict[str, tuple[int, str]] = {}


def test_screw_grid_avoids_expanded_rectangular_part_boundary():
    request = _request(
        geometry=_geometry(
            stock=(0, 0, 6, 4),
            entities=[
                _entity("frame", "rectangle", (0, 0, 6, 4)),
                _entity("e1", "rectangle", (2, 1, 4, 3)),
            ],
            entity_map=[
                {"entity": "frame", "role": "frame", "children": [{"entity": "e1", "role": "part"}]},
            ],
        ),
        screw_grid=0.75,
        screw_clearance=0.3,
        min_screw_distance=1.1,
    )

    points = _screw_points(request)

    blocked = _part_clearance_polygons(request, 0.3)
    assert points
    assert all(not blocked.covers(Point(point)) for point in points)
    assert all(_has_neighbor(point, points, 1.1) for point in points)
    _render_preview("rectangular part clearance", request, points)


def test_screw_grid_uses_part_polygon_instead_of_part_bounding_box():
    fixed_dxf, handle = _l_shape_dxf()
    request = _request(
        geometry=_geometry(
            stock=(0, 0, 5, 4),
            entities=[
                _entity("frame", "rectangle", (0, 0, 5, 4)),
                _entity("e1", "polyline", (1, 1, 4, 3), handle=handle),
            ],
            entity_map=[
                {"entity": "frame", "role": "frame", "children": [{"entity": "e1", "role": "part"}]},
            ],
        ),
        screw_grid=0.5,
        screw_clearance=0.1,
        min_screw_distance=0.75,
        screw_spacing=1.0,
        fixed_dxf=fixed_dxf,
    )

    points = _screw_points(request)

    assert (3.0, 3.0) in points
    blocked = _part_clearance_polygons(request, 0.1)
    assert all(not blocked.covers(Point(point)) for point in points)
    _render_preview("L-shaped part polygon clearance", request, points)


def test_screw_grid_discards_points_without_nearby_screw():
    request = _request(
        geometry=_geometry(
            stock=(0, 0, 2, 2),
            entities=[_entity("frame", "rectangle", (0, 0, 2, 2))],
            entity_map=[{"entity": "frame", "role": "frame"}],
        ),
        screw_grid=1.0,
        screw_clearance=0.0,
        min_screw_distance=0.75,
    )

    points = _screw_points(request)

    assert points == []
    _render_preview("near-neighbor filter", request, points)


def _request(
    geometry: GeometryFile,
    screw_grid: float,
    screw_clearance: float,
    min_screw_distance: float,
    screw_spacing: float | None = None,
    fixed_dxf: str | None = None,
) -> PlanningRequest:
    machine = MachineFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "speed": "in/min"},
            "machine": {
                "name": "fixture test router",
                "type": "router",
                "axes": 3,
                "max_tools": 1,
                "clear_z": 0.25,
                "screw_grid": screw_grid,
                "screw_clearance": screw_clearance,
                "workholding": ["screws"],
                "part_holding": ["tabs"],
                "work_envelope": {"x": {"min": 0, "max": 96}, "y": {"min": 0, "max": 50}, "z": {"min": -3, "max": 0}},
                "coordinate_system": {"x_positive": "right", "y_positive": "up", "origin": "bottom_left"},
                "spindle": {"type": "er11", "max_rpm": 24000},
            },
            "tools": [
                {
                    "id": "t1",
                    "description": "fixture drill",
                    "end_type": "flat",
                    "flute_spiral": "upcut",
                    "diameter": 0.25,
                    "flutes": 2,
                    "speed": 18000,
                    "feed_rate": 80,
                    "plunge_rate": 25,
                    "depth_per_pass": 0.125,
                }
            ],
        }
    )
    return PlanningRequest.model_validate(
        {
            "geometry": geometry.model_dump(mode="json"),
            "machine": machine.model_dump(mode="json"),
            "system_advice": {},
            "user_advice": {},
            "fixed_dxf": fixed_dxf,
            "inputs": {
                "stock_xy": "fixture test",
                "stock_units": "in",
                "stock_thickness": 0.25,
                "stock_material": "plywood",
                "z_zero_position": "stock_top",
                "coordinate_system": "G54",
                "workholding_method": ["screws"],
                "min_screw_distance": min_screw_distance,
                "screw_spacing": screw_spacing,
            },
        }
    )


def _geometry(stock, entities, entity_map) -> GeometryFile:
    min_x, min_y, max_x, max_y = stock
    return GeometryFile.model_validate(
        {
            "schema_version": "1.0",
            "units": {"length": "in", "source": "guessed", "confidence": 1.0, "coordinate_scale": 1.0},
            "source": {"original_file": "fixture.dxf", "cleaned_file": "fixture_fixed.dxf", "format": "dxf"},
            "summary": {
                "entity_count": len(entities),
                "closed_count": len(entities),
                "open_count": 0,
                "bounding_box": _bbox((min_x, min_y, max_x, max_y)),
            },
            "entity_map": entity_map,
            "entities": entities,
        }
    )


def _entity(entity_id: str, shape: str, bounds, handle: str | None = None):
    min_x, min_y, max_x, max_y = bounds
    refs = [{"kind": "dxf_handle", "value": handle}] if handle else []
    return {
        "id": entity_id,
        "type": "closed_loop",
        "shape": shape,
        "source_refs": refs,
        "bounding_box": _bbox(bounds),
        "area": (max_x - min_x) * (max_y - min_y),
        "perimeter": 2 * ((max_x - min_x) + (max_y - min_y)),
    }


def _bbox(bounds):
    min_x, min_y, max_x, max_y = bounds
    return {"min": {"x": min_x, "y": min_y}, "max": {"x": max_x, "y": max_y}}


def _l_shape_dxf() -> tuple[str, str]:
    doc = ezdxf.new("R2000")
    entity = doc.modelspace().add_lwpolyline(
        [(1, 1), (4, 1), (4, 1.75), (2, 1.75), (2, 3), (1, 3)],
        close=True,
    )
    stream = StringIO()
    doc.write(stream)
    return stream.getvalue(), entity.dxf.handle


def _has_neighbor(point, points, distance: float) -> bool:
    return any(point != other and Point(point).distance(Point(other)) <= distance + 1e-9 for other in points)


def _render_preview(title: str, request: PlanningRequest, points: list[tuple[float, float]]) -> None:
    PREVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_SECTIONS[title] = _preview_section(title, request, points)
    PREVIEW_FILE.write_text(_preview_svg(PREVIEW_SECTIONS), encoding="utf-8")


def _preview_section(title: str, request: PlanningRequest, points: list[tuple[float, float]]) -> tuple[int, str]:
    min_x, min_y, max_x, max_y = _frame_or_summary(request)
    scale = 60
    margin = 40
    title_height = 34
    drawing_height = (max_y - min_y) * scale
    section_height = int(title_height + drawing_height + margin * 2)

    def tx(x: float) -> float:
        return margin + (x - min_x) * scale

    def ty(y: float) -> float:
        return title_height + margin + (max_y - y) * scale

    stock = f'<rect class="stock" x="{tx(min_x)}" y="{ty(max_y)}" width="{(max_x-min_x)*scale}" height="{(max_y-min_y)*scale}" />'
    blocked = _part_clearance_polygons(request, request.machine.machine.screw_clearance or 0)
    blocked_paths = []
    if blocked is not None and not blocked.is_empty:
        geoms = list(blocked.geoms) if hasattr(blocked, "geoms") else [blocked]
        for polygon in geoms:
            coords = " ".join(f"{tx(x):.1f},{ty(y):.1f}" for x, y in polygon.exterior.coords)
            blocked_paths.append(f'<polygon class="clearance" points="{coords}" />')
    point_nodes = [
        f'<circle class="screw" cx="{tx(x):.1f}" cy="{ty(y):.1f}" r="4" />'
        for x, y in points
    ]
    body = "\n".join(
        [
            f'<text class="title" x="20" y="24">{escape(title)}</text>',
            '<text class="legend" x="20" y="44">blue: selected screw holes | red: part boundary plus screw clearance</text>',
            stock,
            *blocked_paths,
            *point_nodes,
        ]
    )
    return section_height, body


def _preview_svg(sections: dict[str, tuple[int, str]]) -> str:
    gap = 28
    height = max(320, sum(section_height + gap for section_height, _ in sections.values()) + 20)
    body: list[str] = []
    y_offset = 0
    for title, (section_height, section_body) in sections.items():
        body.extend(
            [
                f"<!-- section:{escape(title)} -->",
                f'<g transform="translate(0 {y_offset})">',
                section_body,
                "</g>",
                "<!-- endsection -->",
            ]
        )
        y_offset += section_height + gap
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="620" height="{height}" viewBox="0 0 620 {height}">',
            "<style>",
            ".title{font:700 18px Arial,sans-serif;fill:#111827}.stock{fill:#fff;stroke:#111827;stroke-width:2}",
            ".legend{font:12px Arial,sans-serif;fill:#475569}",
            ".clearance{fill:#fee2e2;stroke:#ef4444;stroke-width:1.5;fill-opacity:.55}.screw{fill:#2563eb;stroke:#fff;stroke-width:1.5}",
            "</style>",
            '<rect width="100%" height="100%" fill="#fff" />',
            *body,
            "</svg>",
            "",
        ]
    )


def _frame_or_summary(request: PlanningRequest):
    box = request.geometry.summary.bounding_box
    return box.min.x, box.min.y, box.max.x, box.max.y
