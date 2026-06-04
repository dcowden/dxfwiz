from __future__ import annotations

from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import path
from shapely.geometry import Point, Polygon

from dxfwiz.dxf.units import determine_length_units
from dxfwiz.yaml_io import dump_yaml_file


FLATTENING_DISTANCE = 0.01
RECTANGLE_TOLERANCE = 1e-3


def write_geometry_yaml(
    fixed_dxf_path: str | Path,
    geom_yaml_path: str | Path,
    original_file: str,
    cleaned_file: str,
    length_units: str | None = None,
) -> dict[str, Any]:
    fixed_dxf_path = Path(fixed_dxf_path)
    doc = ezdxf.readfile(fixed_dxf_path)
    msp = doc.modelspace()

    entities = []
    polygons = {}
    for index, entity in enumerate(msp, start=1):
        dxftype = entity.dxftype()
        if dxftype == "CIRCLE":
            geom_entity = _circle_entity(index, entity)
            entities.append(geom_entity)
            polygons[geom_entity["id"]] = Point(
                geom_entity["center"]["x"], geom_entity["center"]["y"]
            ).buffer(geom_entity["diameter"] / 2, quad_segs=32)
        elif dxftype == "LWPOLYLINE":
            geom_entity = _lwpolyline_entity(index, entity)
            entities.append(geom_entity)
            polygon = _lwpolyline_polygon(entity)
            if polygon is not None:
                polygons[geom_entity["id"]] = polygon

    closed_count = sum(1 for entity in entities if entity["type"] == "closed_loop")
    open_count = sum(1 for entity in entities if entity["type"] == "open_path")
    bounding_box = _summary_bounds(entities)
    containment_tree = _containment_tree(entities, polygons)
    unit_decision = determine_length_units(
        doc_units=doc.units,
        measurement=doc.header.get("$MEASUREMENT"),
        entities=entities,
        containment_tree=containment_tree,
    )
    if unit_decision.coordinate_scale != 1.0:
        entities = _scale_entities(entities, unit_decision.coordinate_scale)
        bounding_box = _summary_bounds(entities)
    units = unit_decision.to_yaml()
    if length_units is not None:
        units = {
            "length": length_units,
            "source": "explicit_dxf",
            "confidence": 1.0,
            "evidence": ["Length units supplied by caller"],
        }

    data = {
        "schema_version": "1.0",
        "units": units,
        "source": {
            "original_file": original_file,
            "cleaned_file": cleaned_file,
            "format": "dxf",
        },
        "summary": {
            "entity_count": len(entities),
            "closed_count": closed_count,
            "open_count": open_count,
            "bounding_box": bounding_box,
        },
        "entities": entities,
        "containment_tree": containment_tree,
    }
    dump_yaml_file(geom_yaml_path, data)
    return data


def _scale_entities(entities: list[dict[str, Any]], scale: float) -> list[dict[str, Any]]:
    return [_scale_entity(entity, scale) for entity in entities]


def _scale_entity(entity: dict[str, Any], scale: float) -> dict[str, Any]:
    scaled = dict(entity)
    if entity.get("bounding_box"):
        scaled["bounding_box"] = _scale_box(entity["bounding_box"], scale)
    if entity.get("center"):
        scaled["center"] = _scale_point(entity["center"], scale)
    if entity.get("diameter") is not None:
        scaled["diameter"] = entity["diameter"] * scale
    if entity.get("area") is not None:
        scaled["area"] = entity["area"] * scale * scale
    if entity.get("perimeter") is not None:
        scaled["perimeter"] = entity["perimeter"] * scale
    return scaled


def _scale_box(box: dict[str, dict[str, float]], scale: float) -> dict[str, dict[str, float]]:
    return {
        "min": _scale_point(box["min"], scale),
        "max": _scale_point(box["max"], scale),
    }


def _scale_point(point: dict[str, float], scale: float) -> dict[str, float]:
    return {"x": point["x"] * scale, "y": point["y"] * scale}


def _circle_entity(index: int, entity) -> dict[str, Any]:
    center = entity.dxf.center
    radius = float(entity.dxf.radius)
    return {
        "id": f"e{index}",
        "type": "closed_loop",
        "shape": "circle",
        "source_refs": _source_refs(entity),
        "bounding_box": {
            "min": {"x": float(center.x) - radius, "y": float(center.y) - radius},
            "max": {"x": float(center.x) + radius, "y": float(center.y) + radius},
        },
        "center": {"x": float(center.x), "y": float(center.y)},
        "diameter": radius * 2,
    }


def _lwpolyline_entity(index: int, entity) -> dict[str, Any]:
    points = [(float(point[0]), float(point[1]), float(point[4])) for point in entity.get_points()]
    bounds = _point_bounds([(point[0], point[1]) for point in points])
    has_bulges = any(abs(point[2]) > 0 for point in points)
    shape = "polyline_with_arcs" if has_bulges else "polyline"
    if not has_bulges and entity.closed and _is_axis_aligned_rectangle(points):
        shape = "rectangle"
    return {
        "id": f"e{index}",
        "type": "closed_loop" if entity.closed else "open_path",
        "shape": shape,
        "source_refs": _source_refs(entity),
        "bounding_box": bounds,
    }


def _source_refs(entity) -> list[dict[str, str]]:
    refs = [{"kind": "dxf_handle", "value": entity.dxf.handle}]
    try:
        xdata = entity.get_xdata("DXFWIZ")
    except Exception:
        xdata = []
    for code, value in xdata:
        if code == 1000:
            refs.insert(0, {"kind": "dxfwiz_id", "value": str(value)})
            break
    return refs


def _summary_bounds(entities: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    boxes = [entity["bounding_box"] for entity in entities if "bounding_box" in entity]
    if not boxes:
        return {"min": {"x": 0.0, "y": 0.0}, "max": {"x": 0.0, "y": 0.0}}
    points = []
    for box in boxes:
        points.append((box["min"]["x"], box["min"]["y"]))
        points.append((box["max"]["x"], box["max"]["y"]))
    return _point_bounds(points)


def _point_bounds(points: list[tuple[float, float]]) -> dict[str, dict[str, float]]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "min": {"x": min(xs), "y": min(ys)},
        "max": {"x": max(xs), "y": max(ys)},
    }


def _is_axis_aligned_rectangle(points: list[tuple[float, float, float]]) -> bool:
    simplified = _remove_collinear_points([(point[0], point[1]) for point in points])
    if len(simplified) != 4:
        return False
    if len(_cluster_values([point[0] for point in simplified], RECTANGLE_TOLERANCE)) != 2:
        return False
    if len(_cluster_values([point[1] for point in simplified], RECTANGLE_TOLERANCE)) != 2:
        return False
    for start, end in zip(simplified, simplified[1:] + simplified[:1]):
        same_x = abs(start[0] - end[0]) <= RECTANGLE_TOLERANCE
        same_y = abs(start[1] - end[1]) <= RECTANGLE_TOLERANCE
        if same_x == same_y:
            return False
    return True


def _cluster_values(values: list[float], tolerance: float) -> list[float]:
    clusters: list[float] = []
    for value in sorted(values):
        if not clusters or abs(value - clusters[-1]) > tolerance:
            clusters.append(value)
    return clusters


def _remove_collinear_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    simplified = points[:]
    changed = True
    while changed and len(simplified) > 2:
        changed = False
        kept = []
        for index, point in enumerate(simplified):
            previous = simplified[index - 1]
            following = simplified[(index + 1) % len(simplified)]
            if _is_collinear(previous, point, following):
                changed = True
                continue
            kept.append(point)
        simplified = kept
    return simplified


def _is_collinear(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) <= 1e-6


def _lwpolyline_polygon(entity):
    if not entity.closed:
        return None
    coords = _lwpolyline_flat_points(entity)
    if len(coords) < 4:
        return None
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        return None
    return polygon


def _lwpolyline_flat_points(entity) -> list[tuple[float, float]]:
    return [(float(vertex.x), float(vertex.y)) for vertex in path.make_path(entity).flattening(FLATTENING_DISTANCE)]


def _containment_tree(
    entities: list[dict[str, Any]], polygons: dict[str, Polygon]
) -> list[dict[str, Any]]:
    entity_by_id = {entity["id"]: entity for entity in entities}
    parent_by_id: dict[str, str | None] = {entity["id"]: None for entity in entities}

    for child_id, child_polygon in polygons.items():
        possible_parents = []
        for parent_id, parent_polygon in polygons.items():
            if parent_id == child_id:
                continue
            if parent_polygon.area <= child_polygon.area:
                continue
            if parent_polygon.buffer(1e-6).covers(child_polygon):
                possible_parents.append((parent_polygon.area, parent_id))
        if possible_parents:
            parent_by_id[child_id] = min(possible_parents)[1]

    children_by_parent: dict[str | None, list[str]] = {}
    for entity in entities:
        children_by_parent.setdefault(parent_by_id[entity["id"]], []).append(entity["id"])

    root_ids = children_by_parent.get(None, [])
    frame_ids = _frame_ids(root_ids, children_by_parent, entity_by_id)
    semantic_root_ids = [root_id for root_id in root_ids if root_id in frame_ids or root_id not in _empty_frame_ids(root_ids, children_by_parent, entity_by_id)]
    depth_by_id = _depths(parent_by_id)

    def make_node(entity_id: str) -> dict[str, Any]:
        entity = entity_by_id[entity_id]
        children = sorted(
            children_by_parent.get(entity_id, []),
            key=lambda child_id: polygons.get(child_id, Polygon()).area,
            reverse=True,
        )
        return {
            "entity": entity_id,
            "role": _role_for(entity, entity_id, frame_ids, depth_by_id),
            "children": [make_node(child_id) for child_id in children],
        }

    return [
        make_node(entity_id)
        for entity_id in sorted(
            semantic_root_ids,
            key=lambda root_id: polygons.get(root_id, Polygon()).area,
            reverse=True,
        )
    ]


def _frame_ids(
    root_ids: list[str],
    children_by_parent: dict[str | None, list[str]],
    entity_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    return {
        root_id
        for root_id in root_ids
        if _is_strict_rectangle(entity_by_id[root_id])
        and len(children_by_parent.get(root_id, [])) > 0
    }


def _empty_frame_ids(
    root_ids: list[str],
    children_by_parent: dict[str | None, list[str]],
    entity_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    return {
        root_id
        for root_id in root_ids
        if _is_strict_rectangle(entity_by_id[root_id])
        and len(children_by_parent.get(root_id, [])) == 0
    }


def _is_strict_rectangle(entity: dict[str, Any]) -> bool:
    return entity["type"] == "closed_loop" and entity["shape"] == "rectangle"


def _depths(parent_by_id: dict[str, str | None]) -> dict[str, int]:
    depths = {}
    for entity_id in parent_by_id:
        depth = 0
        parent_id = parent_by_id[entity_id]
        while parent_id is not None:
            depth += 1
            parent_id = parent_by_id[parent_id]
        depths[entity_id] = depth
    return depths


def _role_for(
    entity: dict[str, Any],
    entity_id: str,
    frame_ids: set[str],
    depth_by_id: dict[str, int],
) -> str:
    if entity["type"] == "open_path":
        return "uncontained"
    if entity_id in frame_ids:
        return "frame"
    depth = depth_by_id[entity_id]
    if depth == 0:
        return "part"
    if depth == 1:
        return "part" if frame_ids else "cutout"
    if depth == 2:
        return "cutout"
    return "island" if depth % 2 == 1 else "cutout"
