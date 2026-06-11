from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import path as ezdxf_path
from pydantic import Field
from shapely.geometry import Point, Polygon, box

from dxfwiz.schemas import GeometryFile, MachineFile
from dxfwiz.schemas.common import StrictModel
from dxfwiz.schemas.machine import Tool


logger = logging.getLogger(__name__)


class ToolFitRejection(StrictModel):
    tool: str
    entity: str
    reason: str


class ToolSelectionResult(StrictModel):
    tool: Tool
    rejected: list[ToolFitRejection] = Field(default_factory=list)


def select_largest_single_tool(
    geometry: GeometryFile,
    machine: MachineFile,
    fixed_dxf: str | Path | None = None,
) -> ToolSelectionResult:
    """Select the largest tool that fits all required internal features."""
    dxf_lookup = _load_dxf_lookup(fixed_dxf)
    candidates = sorted(machine.tools, key=lambda tool: tool.diameter, reverse=True)
    all_rejections: list[ToolFitRejection] = []
    for tool in candidates:
        rejections = _tool_rejections(tool, geometry, dxf_lookup)
        if not rejections:
            return ToolSelectionResult(tool=tool, rejected=all_rejections)
        all_rejections.extend(rejections)
    fallback = min(machine.tools, key=lambda tool: tool.diameter)
    return ToolSelectionResult(tool=fallback, rejected=all_rejections)


def selected_tool_rejections(
    tool: Tool,
    geometry: GeometryFile,
    fixed_dxf: str | Path | None = None,
) -> list[ToolFitRejection]:
    """Return geometry fit rejections for an explicitly requested tool."""
    return _tool_rejections(tool, geometry, _load_dxf_lookup(fixed_dxf))


def _tool_rejections(tool: Tool, geometry: GeometryFile, dxf_lookup: dict[str, Any]) -> list[ToolFitRejection]:
    entities = {entity.id: entity for entity in geometry.entities}
    rejections: list[ToolFitRejection] = []
    for node in _flatten_nodes([node.model_dump() for node in geometry.entity_map]):
        if node["role"] != "cutout":
            continue
        entity = entities.get(node["entity"])
        if entity is None:
            continue
        rejection = _internal_feature_rejection(tool, entity, dxf_lookup)
        if rejection:
            rejections.append(rejection)
    return rejections


def _internal_feature_rejection(tool: Tool, entity, dxf_lookup: dict[str, Any]) -> ToolFitRejection | None:
    if entity.shape == "circle" and entity.diameter is not None:
        if tool.diameter <= entity.diameter + 1e-9:
            return None
        return ToolFitRejection(
            tool=tool.id,
            entity=entity.id,
            reason=f"tool diameter {tool.diameter:.6f} exceeds circular cutout diameter {entity.diameter:.6f}",
        )
    polygon = _entity_polygon(entity, dxf_lookup)
    if polygon is None:
        return None
    remaining = polygon.buffer(-tool.diameter / 2, join_style="round")
    if not remaining.is_empty and remaining.area > 1e-9:
        return None
    return ToolFitRejection(
        tool=tool.id,
        entity=entity.id,
        reason=f"tool diameter {tool.diameter:.6f} does not fit internal cutout geometry",
    )


def _entity_polygon(entity, dxf_lookup: dict[str, Any]) -> Polygon | None:
    for ref in entity.source_refs:
        if ref.kind != "dxf_handle":
            continue
        dxf_entity = dxf_lookup.get(ref.value)
        polygon = _dxf_polygon(dxf_entity)
        if polygon is not None:
            return polygon
    if entity.shape == "circle" and entity.center is not None and entity.diameter is not None:
        return Point(entity.center.x, entity.center.y).buffer(entity.diameter / 2, quad_segs=48)
    if entity.bounding_box is not None:
        bounds = entity.bounding_box
        return box(bounds.min.x, bounds.min.y, bounds.max.x, bounds.max.y)
    return None


def _dxf_polygon(dxf_entity) -> Polygon | None:
    if dxf_entity is None:
        return None
    if dxf_entity.dxftype() == "CIRCLE":
        center = dxf_entity.dxf.center
        return Point(float(center.x), float(center.y)).buffer(float(dxf_entity.dxf.radius), quad_segs=48)
    try:
        points = [(float(vertex.x), float(vertex.y)) for vertex in ezdxf_path.make_path(dxf_entity).flattening(0.005)]
    except Exception:
        logger.debug("Could not flatten DXF entity %s for tool selection", dxf_entity, exc_info=True)
        return None
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon if not polygon.is_empty else None


def _load_dxf_lookup(fixed_dxf: str | Path | None) -> dict[str, Any]:
    if fixed_dxf is None:
        return {}
    try:
        if isinstance(fixed_dxf, Path):
            doc = ezdxf.readfile(fixed_dxf)
        elif "\n" in fixed_dxf or fixed_dxf.lstrip().startswith("0"):
            doc = ezdxf.read(StringIO(fixed_dxf))
        else:
            doc = ezdxf.readfile(fixed_dxf)
    except Exception:
        logger.exception("Failed to load fixed DXF for tool selection")
        return {}
    return {entity.dxf.handle: entity for entity in doc.modelspace()}


def _flatten_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten_nodes(node.get("children", [])))
    return result
