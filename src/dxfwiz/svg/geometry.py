from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import path

from dxfwiz.yaml_io import load_yaml_file


FLATTENING_DISTANCE = 0.005
MIN_MARKER_RADIUS = 0.035
MAX_MARKER_RADIUS = 0.095
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircleLabel:
    lines: tuple[str, ...]
    part_entity_id: str | None = None
    part_bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class LabelPlacement:
    x: float
    y: float
    box: tuple[float, float, float, float]
    side: str


def render_geometry_svg(
    geom_yaml_path: str | Path,
    fixed_dxf_path: str | Path,
    svg_path: str | Path,
) -> str:
    logger.info("Rendering geometry SVG %s", svg_path)
    geom = load_yaml_file(geom_yaml_path)
    fixed_doc = ezdxf.readfile(fixed_dxf_path)
    entity_by_handle = {entity.dxf.handle: entity for entity in fixed_doc.modelspace()}
    entity_map = _entity_map(geom)
    role_by_entity = _role_map(entity_map)
    circle_labels = _circle_label_map(geom, role_by_entity)
    scale = geom["units"].get("coordinate_scale", 1.0)
    bounds = geom["summary"]["bounding_box"]

    min_x = bounds["min"]["x"]
    min_y = bounds["min"]["y"]
    max_x = bounds["max"]["x"]
    max_y = bounds["max"]["y"]
    width = max(max_x - min_x, 1e-6)
    height = max(max_y - min_y, 1e-6)
    marker_radius = min(max(max(width, height) / 420, MIN_MARKER_RADIUS), MAX_MARKER_RADIUS)
    geometry_boxes = _rendered_entity_boxes(geom["entities"], max_y, marker_radius)
    occupied_label_boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "left": [],
        "right": [],
    }

    geometry_elements = []
    annotation_elements = []
    for geom_entity in geom["entities"]:
        dxf_entity = _fixed_entity_for(geom_entity, entity_by_handle)
        if dxf_entity is None:
            continue
        role = role_by_entity.get(geom_entity["id"], "uncontained")
        classes = f"entity entity-{escape(geom_entity['type'])} shape-{escape(geom_entity['shape'])} role-{escape(role)}"
        rendered = _render_entity(
            dxf_entity=dxf_entity,
            geom_entity=geom_entity,
            scale=scale,
            units=geom["units"]["length"],
            circle_label=circle_labels.get(geom_entity["id"]),
            min_y=min_y,
            max_y=max_y,
            classes=classes,
            marker_radius=marker_radius,
            drawing_bounds=(min_x, 0.0, max_x, height),
            drawing_center_x=(min_x + max_x) / 2,
            geometry_boxes=geometry_boxes,
            occupied_label_boxes=occupied_label_boxes,
        )
        if rendered:
            geometry, annotation = rendered
            geometry_elements.append(geometry)
            if annotation:
                annotation_elements.append(annotation)
    annotation_elements.append(
        _render_entity_name_labels(
            entities=geom["entities"],
            role_by_entity=role_by_entity,
            max_y=max_y,
            marker_radius=marker_radius,
        )
    )

    svg = _svg_document(
        min_x=min_x,
        width=width,
        height=height,
        marker_radius=marker_radius,
        geometry_body="\n    ".join(geometry_elements),
        annotation_body="\n    ".join(annotation_elements),
    )
    svg_path = Path(svg_path)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(svg, encoding="utf-8")
    logger.info("Wrote geometry SVG %s", svg_path)
    return svg


def _fixed_entity_for(geom_entity: dict[str, Any], entity_by_handle: dict[str, Any]):
    for ref in geom_entity["source_refs"]:
        if ref["kind"] == "dxf_handle":
            entity = entity_by_handle.get(ref["value"])
            if entity is not None:
                return entity
    return None


def _render_entity(
    dxf_entity,
    geom_entity: dict[str, Any],
    scale: float,
    units: str,
    circle_label: CircleLabel | None,
    min_y: float,
    max_y: float,
    classes: str,
    marker_radius: float,
    drawing_bounds: tuple[float, float, float, float],
    drawing_center_x: float,
    geometry_boxes: list[tuple[str, tuple[float, float, float, float]]],
    occupied_label_boxes: dict[str, list[tuple[float, float, float, float]]],
) -> tuple[str, str] | None:
    dxftype = dxf_entity.dxftype()
    data_attrs = f'data-entity-id="{escape(geom_entity["id"])}" data-dxf-handle="{escape(dxf_entity.dxf.handle)}"'
    title = _title(geom_entity, dxf_entity)
    if dxftype == "CIRCLE":
        center = dxf_entity.dxf.center
        radius = float(dxf_entity.dxf.radius) * scale
        cx, cy = _point(float(center.x), float(center.y), scale, min_y, max_y)
        geometry = (
            f'<g class="entity-geometry {classes}" {data_attrs}>\n'
            f'    <title>{title}</title>\n'
            f'    <circle class="entity-outline" cx="{cx:.6f}" cy="{cy:.6f}" r="{radius:.6f}" />\n'
            f"  </g>"
        )
        label_text = ""
        if circle_label is not None:
            placement = _place_label(
                anchor=(cx, cy),
                radius=radius,
                label=circle_label,
                marker_radius=marker_radius,
                drawing_bounds=drawing_bounds,
                drawing_center_x=drawing_center_x,
                geometry_boxes=geometry_boxes,
                occupied_label_boxes=occupied_label_boxes,
                entity_id=geom_entity["id"],
            )
            occupied_label_boxes[placement.side].append(placement.box)
            label_text = _render_callout_label(
                label=circle_label,
                placement=placement,
                marker_radius=marker_radius,
                circle_center=(cx, cy),
                circle_radius=radius,
            )
        annotation = (
            f'<g class="entity-annotation circle-annotation" data-entity-ref="{escape(geom_entity["id"])}">\n'
            f'    <circle class="center-marker" cx="{cx:.6f}" cy="{cy:.6f}" r="{marker_radius * 0.75:.6f}" />\n'
            f"{label_text}"
            f"  </g>"
        )
        return geometry, annotation
    if dxftype == "LWPOLYLINE":
        points = [
            _point(float(vertex.x), float(vertex.y), scale, min_y, max_y)
            for vertex in path.make_path(dxf_entity).flattening(FLATTENING_DISTANCE / max(scale, 1e-9))
        ]
        if len(points) < 2:
            return None
        command = "M " + " L ".join(f"{x:.6f} {y:.6f}" for x, y in points)
        if dxf_entity.closed:
            command += " Z"
        vertices = _polyline_vertices(dxf_entity, scale, min_y, max_y)
        vertex_markers = _render_polyline_markers(vertices, marker_radius, closed=dxf_entity.closed)
        geometry = (
            f'<g class="entity-geometry {classes}" {data_attrs}>\n'
            f'    <title>{title}</title>\n'
            f'    <path class="entity-outline" d="{command}" />\n'
            f"  </g>"
        )
        annotation = (
            f'<g class="entity-annotation polyline-annotation" data-entity-ref="{escape(geom_entity["id"])}">\n'
            f"    {vertex_markers}\n"
            f"  </g>"
        )
        return geometry, annotation
    return None


def _polyline_vertices(
    dxf_entity,
    scale: float,
    min_y: float,
    max_y: float,
) -> list[tuple[float, float, float]]:
    vertices = []
    for x, y, _start_width, _end_width, bulge in dxf_entity.get_points("xyseb"):
        px, py = _point(float(x), float(y), scale, min_y, max_y)
        vertices.append((px, py, float(bulge)))
    return vertices


def _render_polyline_markers(
    vertices: list[tuple[float, float, float]],
    marker_radius: float,
    closed: bool,
) -> str:
    if not vertices:
        return ""
    markers = []
    for index, (x, y, bulge) in enumerate(vertices):
        if not closed and index == 0:
            markers.append(_triangle_marker(x, y, marker_radius * 2.45, "entity-start-marker"))
        elif not closed and index == len(vertices) - 1:
            markers.append(_square_marker(x, y, marker_radius * 1.85, "entity-end-marker"))
        else:
            markers.append(
                f'<circle class="segment-marker segment-junction-marker" cx="{x:.6f}" cy="{y:.6f}" r="{marker_radius:.6f}" />'
            )
    if closed:
        x, y, _bulge = vertices[0]
        markers.append(_triangle_marker(x, y, marker_radius * 2.45, "entity-start-marker"))
    return "\n    ".join(markers)


def _triangle_marker(x: float, y: float, size: float, marker_class: str) -> str:
    points = [
        (x, y - size),
        (x + size * 0.9, y + size * 0.65),
        (x - size * 0.9, y + size * 0.65),
    ]
    return f'<path class="segment-marker {marker_class}" d="{_polygon_path(points)}" />'


def _square_marker(x: float, y: float, size: float, marker_class: str) -> str:
    points = [
        (x - size, y - size),
        (x + size, y - size),
        (x + size, y + size),
        (x - size, y + size),
    ]
    return f'<path class="segment-marker {marker_class}" d="{_polygon_path(points)}" />'


def _polygon_path(points: list[tuple[float, float]]) -> str:
    return (
        "M "
        + " L ".join(f"{x:.6f} {y:.6f}" for x, y in points)
        + " Z"
    )


def _title(geom_entity: dict[str, Any], dxf_entity) -> str:
    parts = [
        f"id={geom_entity['id']}",
        f"dxf={dxf_entity.dxftype()}",
        f"shape={geom_entity['shape']}",
        f"type={geom_entity['type']}",
        f"handle={dxf_entity.dxf.handle}",
    ]
    if dxf_entity.dxftype() == "LWPOLYLINE":
        vertices = list(dxf_entity.get_points("xyseb"))
        arc_segments = sum(1 for point in vertices if abs(float(point[4])) > 1e-9)
        parts.extend(
            [
                f"vertices={len(vertices)}",
                f"arc_segments={arc_segments}",
                f"line_segments={max(len(vertices) - arc_segments, 0)}",
                f"closed={dxf_entity.closed}",
            ]
        )
    if geom_entity.get("diameter") is not None:
        parts.append(f"diameter={geom_entity['diameter']:.4f}")
    if geom_entity.get("area") is not None:
        parts.append(f"area={geom_entity['area']:.4f}")
    return escape("; ".join(parts))


def _point(
    x: float,
    y: float,
    scale: float,
    min_y: float,
    max_y: float,
) -> tuple[float, float]:
    sx = x * scale
    sy = y * scale
    return sx, max_y - sy


def _role_map(nodes: list[dict[str, Any]]) -> dict[str, str]:
    roles: dict[str, str] = {}

    def visit(node: dict[str, Any]) -> None:
        roles[node["entity"]] = node["role"]
        for child in node.get("children", []):
            visit(child)

    for node in nodes:
        visit(node)
    return roles


def _entity_map(geom: dict[str, Any]) -> list[dict[str, Any]]:
    return geom.get("entity_map", geom.get("containment_tree", []))


def _circle_label_map(
    geom: dict[str, Any],
    role_by_entity: dict[str, str],
) -> dict[str, CircleLabel]:
    entities = {entity["id"]: entity for entity in geom["entities"]}
    labels: dict[str, CircleLabel] = {}

    for part in _part_nodes(_entity_map(geom)):
        part_entity = entities.get(part["entity"])
        if part_entity is None:
            continue
        part_center = _bounds_center(part_entity.get("bounding_box"))
        groups: dict[float, list[dict[str, Any]]] = {}
        for child in part.get("children", []):
            entity = entities.get(child["entity"])
            if (
                entity is None
                or entity.get("shape") != "circle"
                or role_by_entity.get(entity["id"]) != "cutout"
                or entity.get("diameter") is None
            ):
                continue
            groups.setdefault(round(float(entity["diameter"]), 3), []).append(entity)

        for diameter, circles in groups.items():
            label_entity = min(
                circles,
                key=lambda entity: _distance_squared(_entity_center(entity), part_center),
            )
            label = _diameter_label(diameter, len(circles), geom["units"]["length"])
            labels[label_entity["id"]] = CircleLabel(
                lines=label.lines,
                part_entity_id=part_entity["id"],
                part_bounds=_rendered_bounds(part_entity.get("bounding_box"), geom["summary"]["bounding_box"]["max"]["y"]),
            )

    for entity in geom["entities"]:
        if (
            entity.get("shape") == "circle"
            and role_by_entity.get(entity["id"]) != "cutout"
            and entity.get("diameter") is not None
        ):
            labels.setdefault(
                entity["id"],
                _diameter_label(round(float(entity["diameter"]), 3), 1, geom["units"]["length"]),
            )
    return labels


def _part_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts = []

    def visit(node: dict[str, Any]) -> None:
        if node["role"] == "part":
            parts.append(node)
        for child in node.get("children", []):
            visit(child)

    for node in nodes:
        visit(node)
    return parts


def _diameter_label(diameter: float, count: int, units: str) -> CircleLabel:
    lines = [f"⌀ {diameter:.3f} {units}"]
    if count > 1:
        lines.append(f"{count} places")
    return CircleLabel(tuple(lines))


def _rendered_entity_boxes(
    entities: list[dict[str, Any]],
    max_y: float,
    padding: float,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    boxes = []
    for entity in entities:
        bounds = entity.get("bounding_box")
        if bounds is None:
            continue
        x1, y1, x2, y2 = _rendered_bounds(bounds, max_y)
        boxes.append((entity["id"], (x1 - padding, y1 - padding, x2 + padding, y2 + padding)))
    return boxes


def _rendered_bounds(bounds: dict[str, Any] | None, max_y: float) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    return (
        bounds["min"]["x"],
        max_y - bounds["max"]["y"],
        bounds["max"]["x"],
        max_y - bounds["min"]["y"],
    )


def _place_label(
    anchor: tuple[float, float],
    radius: float,
    label: CircleLabel,
    marker_radius: float,
    drawing_bounds: tuple[float, float, float, float],
    drawing_center_x: float,
    geometry_boxes: list[tuple[str, tuple[float, float, float, float]]],
    occupied_label_boxes: dict[str, list[tuple[float, float, float, float]]],
    entity_id: str,
) -> LabelPlacement:
    font_size = marker_radius * 2.8
    line_height = font_size * 1.18
    text_width = max(len(line) for line in label.lines) * font_size * 0.56
    text_height = line_height * len(label.lines)
    padding = marker_radius * 1.8
    label_gap = marker_radius * 2.4
    min_x, min_y, max_x, max_y = drawing_bounds
    bubble_width = text_width + padding * 2
    bubble_height = text_height + padding * 2
    side = "left" if anchor[0] < drawing_center_x else "right"
    bubble_x = (
        min_x - marker_radius * 10 - bubble_width
        if side == "left"
        else max_x + marker_radius * 10
    )
    preferred_y = anchor[1] - bubble_height / 2
    candidates = _callout_y_candidates(
        preferred_y=preferred_y,
        bubble_height=bubble_height,
        label_gap=label_gap,
        min_y=min_y,
        max_y=max_y,
    )
    placements = []
    for y in candidates:
        box = (bubble_x, y, bubble_x + bubble_width, y + bubble_height)
        placements.append(
            LabelPlacement(
                x=bubble_x + padding,
                y=y + padding + font_size,
                box=box,
                side=side,
            )
        )
    return min(
        placements,
        key=lambda placement: _label_score(
            placement.box,
            drawing_bounds,
            geometry_boxes,
            occupied_label_boxes[side],
            entity_id,
            label.part_entity_id,
            label.part_bounds,
        ),
    )


def _callout_y_candidates(
    preferred_y: float,
    bubble_height: float,
    label_gap: float,
    min_y: float,
    max_y: float,
) -> list[float]:
    low = min_y + label_gap
    high = max(max_y - bubble_height - label_gap, low)
    step = bubble_height + label_gap
    candidates = [min(max(preferred_y, low), high)]
    for index in range(1, 18):
        candidates.append(min(max(preferred_y - step * index, low), high))
        candidates.append(min(max(preferred_y + step * index, low), high))
    return candidates


def _label_score(
    box: tuple[float, float, float, float],
    drawing_bounds: tuple[float, float, float, float],
    geometry_boxes: list[tuple[str, tuple[float, float, float, float]]],
    occupied_label_boxes: list[tuple[float, float, float, float]],
    entity_id: str,
    parent_part_id: str | None,
    part_bounds: tuple[float, float, float, float] | None,
) -> float:
    score = 0.0
    for label_box in occupied_label_boxes:
        score += _overlap_area(box, label_box) * 2000
    _min_x, min_y, _max_x, max_y = drawing_bounds
    score += max(0.0, min_y - box[1]) * 200
    score += max(0.0, box[3] - max_y) * 200
    return score


def _outside_distance(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    return (
        max(0.0, outer[0] - inner[0])
        + max(0.0, outer[1] - inner[1])
        + max(0.0, inner[2] - outer[2])
        + max(0.0, inner[3] - outer[3])
    )


def _overlap_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return width * height


def _render_callout_label(
    label: CircleLabel,
    placement: LabelPlacement,
    marker_radius: float,
    circle_center: tuple[float, float],
    circle_radius: float,
) -> str:
    line_height = marker_radius * 2.8 * 1.18
    x = placement.x
    y = placement.y
    tspans = []
    for index, line in enumerate(label.lines):
        dy = 0 if index == 0 else line_height
        tspans.append(
            f'<tspan x="{x:.6f}" dy="{dy:.6f}">{escape(line)}</tspan>'
        )
    bubble_x1, bubble_y1, bubble_x2, bubble_y2 = placement.box
    bubble_width = bubble_x2 - bubble_x1
    bubble_height = bubble_y2 - bubble_y1
    leader_start = (bubble_x1, (bubble_y1 + bubble_y2) / 2)
    leader_end = _circle_point_toward(circle_center, circle_radius, leader_start)
    return (
        f'    <path class="leader-line" d="M {leader_start[0]:.6f} {leader_start[1]:.6f} L {leader_end[0]:.6f} {leader_end[1]:.6f}" marker-end="url(#leader-arrow)" />\n'
        f'    <rect class="callout-bubble" x="{bubble_x1:.6f}" y="{bubble_y1:.6f}" width="{bubble_width:.6f}" height="{bubble_height:.6f}" rx="{marker_radius * 1.3:.6f}" />\n'
        f'    <text class="diameter-label" x="{x:.6f}" y="{y:.6f}">'
        + "".join(tspans)
        + "</text>\n"
    )


def _circle_point_toward(
    center: tuple[float, float],
    radius: float,
    target: tuple[float, float],
) -> tuple[float, float]:
    dx = target[0] - center[0]
    dy = target[1] - center[1]
    length = max((dx * dx + dy * dy) ** 0.5, 1e-9)
    return center[0] + dx / length * radius, center[1] + dy / length * radius


def _bounds_center(bounds: dict[str, Any] | None) -> tuple[float, float]:
    if bounds is None:
        return 0.0, 0.0
    return (
        (bounds["min"]["x"] + bounds["max"]["x"]) / 2,
        (bounds["min"]["y"] + bounds["max"]["y"]) / 2,
    )


def _entity_center(entity: dict[str, Any]) -> tuple[float, float]:
    center = entity.get("center")
    if center is not None:
        return center["x"], center["y"]
    return _bounds_center(entity.get("bounding_box"))


def _distance_squared(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _render_entity_name_labels(
    entities: list[dict[str, Any]],
    role_by_entity: dict[str, str],
    max_y: float,
    marker_radius: float,
) -> str:
    labels = []
    font_size = marker_radius * 5.2
    offset = marker_radius * 7.5
    for index, entity in enumerate(entities):
        role = role_by_entity.get(entity["id"], "uncontained")
        if role in {"frame", "ignored"}:
            continue
        bounds = entity.get("bounding_box")
        if bounds is None:
            continue
        x1, y1, x2, y2 = _rendered_bounds(bounds, max_y)
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        angle_slot = index % 4
        dx = (-offset if angle_slot in {0, 3} else offset)
        dy = (-offset if angle_slot in {0, 1} else offset)
        lx = cx + dx
        ly = cy + dy
        labels.append(
            f'<g class="entity-name-label" data-entity-ref="{escape(entity["id"])}">\n'
            f'  <path class="entity-name-leader" d="M {lx:.6f} {ly:.6f} L {cx:.6f} {cy:.6f}" marker-end="url(#entity-name-arrow)" />\n'
            f'  <text class="entity-name-text" x="{lx:.6f}" y="{ly:.6f}" font-size="{font_size:.6f}px">{escape(entity["id"])}</text>\n'
            f"</g>"
        )
    return "\n    ".join(labels)


def _svg_document(
    min_x: float,
    width: float,
    height: float,
    marker_radius: float,
    geometry_body: str,
    annotation_body: str,
) -> str:
    legend_gutter = max(width * 0.24, marker_radius * 62)
    callout_gutter = max(width * 0.22, marker_radius * 70)
    view_min_x = min_x - legend_gutter
    view_width = width + legend_gutter + callout_gutter
    legend_x = view_min_x + legend_gutter * 0.06
    legend_y = height * 0.065
    legend_line = legend_gutter * 0.13
    legend_text_x = legend_x + legend_gutter * 0.19
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_min_x:.6f} 0 {view_width:.6f} {height:.6f}" role="img">
  <defs>
    <marker id="leader-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
      <path d="M 0 0 L 8 4 L 0 8 z" fill="#475569" />
    </marker>
    <marker id="entity-name-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
      <path d="M 0 0 L 8 4 L 0 8 z" fill="#16a34a" />
    </marker>
  </defs>
  <style>
    .entity-outline {{
      fill: none;
      stroke: #374151;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 1.35px;
      vector-effect: non-scaling-stroke;
    }}
    .role-frame .entity-outline {{ stroke: #2563eb; stroke-width: 1.4px; stroke-dasharray: 8 5; }}
    .role-part .entity-outline {{ stroke: #111827; stroke-width: 2.6px; }}
    .role-cutout .entity-outline {{ stroke: #64748b; stroke-width: 1.55px; }}
    .role-cutout.shape-circle .entity-outline {{ stroke-width: 1.15px; }}
    .role-island .entity-outline {{ stroke: #15803d; stroke-width: 1.8px; }}
    .role-uncontained .entity-outline {{ stroke: #7c3aed; stroke-width: 1.6px; stroke-dasharray: 5 4; }}
    .role-ignored .entity-outline {{ stroke: #94a3b8; stroke-width: 1.2px; stroke-dasharray: 6 4; opacity: 0.45; }}
    .role-part.shape-polyline_with_arcs .entity-outline {{ stroke: #111827; }}
    .segment-marker {{
      fill: #f59e0b;
      stroke: #ffffff;
      stroke-width: 1.2px;
      vector-effect: non-scaling-stroke;
    }}
    .entity-start-marker,
    .entity-end-marker {{
      fill: #7c3aed;
    }}
    .center-marker {{
      fill: #f59e0b;
      stroke: #ffffff;
      stroke-width: 1.2px;
      vector-effect: non-scaling-stroke;
    }}
    .diameter-label {{
      font-family: Segoe UI, Arial, sans-serif;
      font-size: {marker_radius * 2.8:.6f}px;
      fill: #111827;
    }}
    .leader-line {{
      fill: none;
      stroke: #475569;
      stroke-width: 0.75px;
      vector-effect: non-scaling-stroke;
    }}
    .callout-bubble {{
      fill: #ffffff;
      stroke: #94a3b8;
      stroke-width: 0.65px;
      vector-effect: non-scaling-stroke;
    }}
    .legend-label {{
      font-family: Segoe UI, Arial, sans-serif;
      font-size: {marker_radius * 3.8:.6f}px;
      fill: #111827;
    }}
    .entity-name-text {{
      font-family: Segoe UI, Arial, sans-serif;
      font-weight: 800;
      fill: #16a34a;
      paint-order: stroke;
      stroke: #ffffff;
      stroke-width: 2.5px;
      vector-effect: non-scaling-stroke;
    }}
    .entity-name-leader {{
      fill: none;
      stroke: #16a34a;
      stroke-width: 0.9px;
      vector-effect: non-scaling-stroke;
    }}
    .legend-swatch {{
      fill: none;
      stroke-width: 2px;
      vector-effect: non-scaling-stroke;
    }}
  </style>
  <g id="geometry-layer">
    {geometry_body}
  </g>
  <g id="annotation-layer">
    {annotation_body}
  </g>
  <g id="legend-layer" opacity="0.94">
    <path class="legend-swatch" d="M {legend_x:.6f} {legend_y:.6f} l {legend_line:.6f} 0" stroke="#111827" />
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius:.6f}">part</text>
    <path class="legend-swatch" d="M {legend_x:.6f} {legend_y + marker_radius * 4:.6f} l {legend_line:.6f} 0" stroke="#64748b" />
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius * 5:.6f}">cutout</text>
    <path class="legend-swatch" d="M {legend_x:.6f} {legend_y + marker_radius * 8:.6f} l {legend_line:.6f} 0" stroke="#2563eb" stroke-dasharray="8 5" />
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius * 9:.6f}">frame</text>
    <circle class="segment-marker" cx="{legend_x + marker_radius:.6f}" cy="{legend_y + marker_radius * 12:.6f}" r="{marker_radius:.6f}" />
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius * 13:.6f}">junction</text>
    {_triangle_marker(legend_x + marker_radius, legend_y + marker_radius * 16, marker_radius * 2.45, "entity-start-marker")}
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius * 17:.6f}">entity start</text>
    {_square_marker(legend_x + marker_radius, legend_y + marker_radius * 20, marker_radius * 1.55, "entity-end-marker")}
    <text class="legend-label" x="{legend_text_x:.6f}" y="{legend_y + marker_radius * 21:.6f}">entity end</text>
  </g>
</svg>
'''
