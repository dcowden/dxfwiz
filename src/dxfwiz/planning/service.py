from __future__ import annotations

import logging
import math
from io import StringIO
from importlib.resources import files
from typing import Any, Literal

import ezdxf
from pydantic import Field
from ruamel.yaml import YAML

from dxfwiz.config import load_config
from dxfwiz.schemas import GeometryFile, JobFile, MachineFile, PlannerFile
from dxfwiz.schemas.common import StrictModel, Units
from dxfwiz.schemas.job import JobInfo, Stock
from dxfwiz.schemas.planner import OperationAdvice
from dxfwiz.planning.yaml_format import dump_operation_yaml


logger = logging.getLogger(__name__)


class PlanningIssue(StrictModel):
    code: str
    message: str
    field: str | None = None
    entity: str | None = None


class PlannerInputs(StrictModel):
    stock_xy: str | None = None
    stock_units: Literal["in", "mm"] | None = None
    stock_thickness: float | None = Field(default=None, gt=0)
    stock_material: str | None = None
    z_zero_position: Literal["stock_top", "spoilboard_top"] | None = None
    coordinate_system: str | None = None
    workholding_method: list[str] = Field(default_factory=list)
    tools: str | list[str] | None = None
    planning_notes: str | None = None
    finishing_allowance: float = Field(default=0.0, ge=0)
    cut_deeper_than_stock: float = Field(default=0.0, ge=0)
    screw_spacing: float | None = Field(default=None, gt=0)


class PlanningRequest(StrictModel):
    geometry: GeometryFile
    machine: MachineFile
    system_advice: OperationAdvice
    user_advice: OperationAdvice
    inputs: PlannerInputs
    fixed_dxf: str | None = None


class PlanningResponse(StrictModel):
    errors: list[PlanningIssue] = Field(default_factory=list)
    warnings: list[PlanningIssue] = Field(default_factory=list)
    geometry: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    op_yaml: str = ""


TAB_TARGET_COUNT = 4


def load_system_planner_advice() -> OperationAdvice:
    advice_path = files("dxfwiz.planning").joinpath("system_planner_advice.yaml")
    logger.debug("Loading system planner advice from %s", advice_path)
    data = YAML(typ="safe").load(advice_path.read_text(encoding="utf-8"))
    return OperationAdvice.model_validate(data["operation_advice"])


def generate_operation_plan(request: PlanningRequest, client: Any | None = None) -> PlanningResponse:
    logger.info("Generating operation plan")
    errors = _validate_required_inputs(request.inputs)
    warnings = _planning_warnings(request)
    if errors:
        logger.info("Operation plan has %d blocking input error(s)", len(errors))
        return PlanningResponse(
            errors=errors,
            warnings=warnings,
            geometry=request.geometry.model_dump(mode="json", exclude_none=True),
            plan=None,
            op_yaml="",
        )

    planner_client = client or _planner_client_from_config()
    response = planner_client.generate(request)
    response.warnings = [*warnings, *response.warnings]
    if response.plan:
        repair_warnings = _normalize_operation_plan(request, response.plan)
        response.warnings = [*response.warnings, *repair_warnings]
        job = JobFile.model_validate(response.plan)
        plan_data = job.model_dump(mode="json", exclude_none=True)
        response.plan = plan_data
        response.geometry = _geometry_with_generated_entities(request.geometry, plan_data)
        response.op_yaml = _dump_yaml(plan_data)
        logger.info(
            "Generated operation plan with %d operation(s) and %d warning(s)",
            len(job.operations),
            len(response.warnings),
        )
    return response


def _planner_client_from_config():
    from dxfwiz.planning.ai import GeminiPlannerClient

    config = load_config()
    if config.planner.mode == "local":
        return LocalPlannerClient()
    return GeminiPlannerClient(config.gemini)


def _normalize_operation_plan(request: PlanningRequest, plan: dict[str, Any]) -> list[PlanningIssue]:
    part_ids = {
        node["entity"]
        for node in _flatten_nodes([node.model_dump() for node in request.geometry.entity_map])
        if node.get("role") == "part"
    }
    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        return []

    warnings: list[PlanningIssue] = []
    finish_entities: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or not _is_outside_part_contour(operation, part_ids):
            continue
        if _is_finish_contour(operation):
            finish_entities.add(operation["entity"])

    new_operations: list[dict[str, Any]] = []
    removed_finish_count = 0
    updated_finish_count = 0
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if _is_outside_part_contour(operation, part_ids) and _is_finish_contour(operation):
            removed_finish_count += 1
            continue
        if _normalize_operation_fields(request, operation, finish_entities, part_ids):
            updated_finish_count += 1
        new_operations.append(operation)

    plan["operations"] = new_operations
    _normalize_operation_groups(plan, [op["id"] for op in new_operations if op.get("type") == "contour"], [])
    if removed_finish_count:
        warnings.append(
            PlanningIssue(
                code="separate_finish_contours_merged",
                message=(
                    f"Merged {removed_finish_count} separate finish contour operation(s) into their parent "
                    "contour operation finishing settings."
                ),
            )
        )
    if updated_finish_count and request.inputs.finishing_allowance > 0:
        warnings.append(
            PlanningIssue(
                code="contour_finishing_settings_repaired",
                message="Updated contour operation finishing settings to match the requested finishing allowance.",
            )
        )
    return warnings


def _is_outside_part_contour(operation: dict[str, Any], part_ids: set[str]) -> bool:
    return (
        operation.get("type") == "contour"
        and operation.get("offset") == "outside"
        and operation.get("entity") in part_ids
    )


def _is_finish_contour(operation: dict[str, Any]) -> bool:
    finishing = operation.get("finishing") or {}
    if finishing.get("enabled"):
        return "finish" in str(operation.get("description", "")).lower()
    finishing_pass = operation.get("finishing_pass") or {}
    if finishing_pass.get("enabled"):
        return True
    return "finish" in str(operation.get("description", "")).lower()


def _normalize_operation_fields(
    request: PlanningRequest,
    operation: dict[str, Any],
    finish_entities: set[str],
    part_ids: set[str],
) -> bool:
    tool = _tool_for_operation(request, operation)
    depth_per_pass = operation.pop("depth_per_pass", None) or tool.depth_per_pass or tool.diameter
    milling_direction = operation.pop("milling_direction", None) or _milling_direction(
        request.inputs.stock_material or "",
        tool.flute_spiral,
    )
    feed_rate = operation.get("feed_rate")
    plunge_rate = operation.get("plunge_rate")
    if operation["type"] == "contour":
        side_allowance = operation.pop("finishing_allowance", None)
        if side_allowance is None:
            side_allowance = request.inputs.finishing_allowance
        operation.pop("finishing_pass", None)
        operation.setdefault("offset", "outside")
        if operation["offset"] == "none":
            operation["offset"] = "on"
        operation.setdefault("extra_depth", 0.0)
        operation["roughing"] = {
            **operation.get("roughing", {}),
            "enabled": True,
            "depth_per_pass": depth_per_pass,
            "side_allowance": side_allowance,
            "bottom_allowance": 0.0,
            "milling_direction": operation.get("roughing", {}).get("milling_direction", milling_direction),
        }
        finishing_required = (
            operation.get("entity") in finish_entities
            or (request.inputs.finishing_allowance > 0 and operation.get("entity") in part_ids and operation.get("offset") == "outside")
        )
        operation["finishing"] = {
            **operation.get("finishing", {}),
            "enabled": bool(operation.get("finishing", {}).get("enabled") or finishing_required),
            "side": True,
            "bottom": False,
            "passes": operation.get("finishing", {}).get("passes", 1),
            "milling_direction": operation.get("finishing", {}).get("milling_direction") or "climb",
        }
        return finishing_required
    if operation["type"] == "pocket":
        finishing_pass = operation.pop("finishing_pass", None) or {}
        operation["roughing"] = {
            **operation.get("roughing", {}),
            "enabled": True,
            "depth_per_pass": depth_per_pass,
            "side_allowance": operation.get("roughing", {}).get("side_allowance", request.inputs.finishing_allowance),
            "bottom_allowance": operation.get("roughing", {}).get("bottom_allowance", request.inputs.finishing_allowance),
            "milling_direction": operation.get("roughing", {}).get("milling_direction", milling_direction),
        }
        operation["finishing"] = {
            **operation.get("finishing", {}),
            "enabled": bool(operation.get("finishing", {}).get("enabled", finishing_pass.get("enabled", True))),
            "side": True,
            "bottom": True,
            "passes": operation.get("finishing", {}).get("passes", 1),
            "milling_direction": operation.get("finishing", {}).get("milling_direction") or "climb",
        }
    if operation["type"] == "helical_drill":
        operation.setdefault("pitch", depth_per_pass)
        operation.setdefault("milling_direction", milling_direction)
        operation["finishing"] = {
            **operation.get("finishing", {}),
            "enabled": operation.get("finishing", {}).get("enabled", True),
            "side": True,
            "bottom": False,
            "passes": operation.get("finishing", {}).get("passes", 1),
            "milling_direction": operation.get("finishing", {}).get("milling_direction") or milling_direction,
        }
    if operation["type"] == "drill":
        operation.setdefault("peck_depth", min(operation["depth"], depth_per_pass))
        operation.setdefault("retract_amount", request.machine.machine.clear_z)
    if feed_rate is not None:
        operation["feed_rate"] = feed_rate
    if plunge_rate is not None:
        operation["plunge_rate"] = plunge_rate
    return False


def _tool_for_operation(request: PlanningRequest, operation: dict[str, Any]):
    machine_tools = {tool.id: tool for tool in request.machine.tools}
    return machine_tools.get(operation.get("tool")) or _best_tool(request)


def _normalize_operation_groups(plan: dict[str, Any], contour_ids: list[str], finish_ids: list[str]) -> None:
    groups = plan.get("operation_groups")
    if not isinstance(groups, list):
        groups = []
        plan["operation_groups"] = groups
    normalized: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = _canonical_group_name(str(group.get("name", "")))
        if name == "finish_contours":
            continue
        operations = [str(op_id) for op_id in group.get("operations", [])]
        if name in normalized:
            _append_unique(normalized[name]["operations"], operations)
            continue
        normalized[name] = {"name": name, "operations": operations}
        result.append(normalized[name])
    contours = normalized.setdefault("contours", {"name": "contours", "operations": []})
    if contours not in result:
        result.append(contours)
    _append_unique(contours["operations"], contour_ids)
    plan["operation_groups"] = result


def _canonical_group_name(name: str) -> str:
    value = name.strip().lower().replace(" ", "_").replace("-", "_")
    if value in {"outer_contours", "outer_contour", "contour"}:
        return "contours"
    if value in {"finishing_contours", "finishing_contour", "finish_contour"}:
        return "finish_contours"
    return value or "operations"


def _append_unique(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value not in seen:
            target.append(value)
            seen.add(value)


class LocalPlannerClient:
    def generate(self, request: PlanningRequest) -> PlanningResponse:
        logger.info("Using local deterministic operation planner")
        warnings: list[PlanningIssue] = []
        plan = _build_plan(request, warnings)
        job = JobFile.model_validate(plan)
        plan_data = job.model_dump(mode="json", exclude_none=True)
        return PlanningResponse(
            errors=[],
            warnings=warnings,
            geometry=_geometry_with_generated_entities(request.geometry, plan_data),
            plan=plan_data,
            op_yaml=_dump_yaml(plan_data),
        )


def _validate_required_inputs(inputs: PlannerInputs) -> list[PlanningIssue]:
    required = [
        ("stock_xy", inputs.stock_xy, "Stock size is required."),
        ("stock_thickness", inputs.stock_thickness, "Stock thickness is required."),
        ("stock_material", inputs.stock_material, "Stock material is required."),
        ("z_zero_position", inputs.z_zero_position, "Z-zero position is required."),
        ("coordinate_system", inputs.coordinate_system, "Coordinate system is required."),
        ("workholding_method", inputs.workholding_method, "Workholding method is required."),
    ]
    errors = []
    for field, value, message in required:
        if value is None or value == "" or value == []:
            errors.append(PlanningIssue(code="missing_required_input", field=field, message=message))
    return errors


def _planning_warnings(request: PlanningRequest) -> list[PlanningIssue]:
    warnings: list[PlanningIssue] = []
    selected_tools = _selected_tool_ids(request)
    machine_tools = {tool.id: tool for tool in request.machine.tools}
    missing_tools = [tool_id for tool_id in selected_tools if tool_id not in machine_tools]
    for tool_id in missing_tools:
        warnings.append(
            PlanningIssue(
                code="unknown_tool",
                field="tools",
                message=f"Selected tool {tool_id} is not in machine.yaml and will be ignored.",
            )
        )
    usable_tool_count = len([tool_id for tool_id in selected_tools if tool_id in machine_tools])
    if usable_tool_count > request.machine.machine.max_tools:
        warnings.append(
            PlanningIssue(
                code="tool_change_required",
                field="tools",
                message="Selected tools exceed machine max_tools; manual tool changes or replanning are required.",
            )
        )
    if "tabs" in request.machine.machine.part_holding:
        warnings.append(
            PlanningIssue(
                code="tabs_not_fully_placed",
                message="Tab placement is advisory in this first planner pass; verify straight segments before cutting.",
            )
        )
    if request.inputs.planning_notes:
        warnings.append(
            PlanningIssue(
                code="notes_not_interpreted",
                field="planning_notes",
                message="Additional instructions are preserved for review but not fully interpreted by the first-pass planner.",
            )
        )
    close_part_warning = _close_part_warning(request)
    if close_part_warning is not None:
        warnings.append(close_part_warning)
    return warnings


def _close_part_warning(request: PlanningRequest) -> PlanningIssue | None:
    boxes = _part_boxes(request.geometry)
    if len(boxes) < 2:
        return None
    tool = _best_tool(request)
    min_gap = min(
        _box_gap(first, second)
        for index, first in enumerate(boxes)
        for second in boxes[index + 1 :]
    )
    if min_gap >= tool.diameter:
        return None
    return PlanningIssue(
        code="parts_too_close",
        message=(
            f"Some parts are only {min_gap:.3f} {request.geometry.units.length} apart, "
            f"which is less than selected tool diameter {tool.diameter:.3f}. "
            "Verify nesting clearance before generating toolpaths."
        ),
    )


def _box_gap(first, second) -> float:
    dx = max(first.min.x - second.max.x, second.min.x - first.max.x, 0.0)
    dy = max(first.min.y - second.max.y, second.min.y - first.max.y, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _build_plan(request: PlanningRequest, warnings: list[PlanningIssue]) -> dict[str, Any]:
    machine = request.machine
    geometry = request.geometry
    inputs = request.inputs
    tool = _best_tool(request)
    material = inputs.stock_material or "unknown"
    stock_thickness = inputs.stock_thickness or 0.0
    milling_direction = _milling_direction(material, tool.flute_spiral)
    entities = {entity.id: entity for entity in geometry.entities}
    nodes = _flatten_nodes([node.model_dump() for node in geometry.entity_map])
    operations: list[dict[str, Any]] = []
    generated_entities: list[dict[str, Any]] = []
    groups: dict[str, list[str]] = {
        "fixtures": [],
        "internal_pockets": [],
        "internal_holes": [],
        "contours": [],
    }
    cut_depth = stock_thickness

    if "screws" in inputs.workholding_method:
        for entity in _screw_entities(geometry, tool.diameter, inputs.screw_spacing):
            generated_entities.append(entity)
            op = _screw_drill_operation(
                len(operations) + 1,
                entity["id"],
                tool.id,
                min(stock_thickness, tool.depth_per_pass or stock_thickness),
                machine.machine.clear_z,
                tool,
            )
            operations.append(op)
            groups["fixtures"].append(op["id"])

    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] in {"frame", "ignored", "uncontained"}:
            continue
        if node["role"] == "cutout" and entity.shape != "circle":
            op = _pocket_operation(len(operations) + 1, entity.id, tool.id, cut_depth, milling_direction)
            operations.append(op)
            groups["internal_pockets"].append(op["id"])
    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] != "cutout" or entity.shape != "circle":
            continue
        op = _hole_operation(len(operations) + 1, entity, tool.id, cut_depth, milling_direction)
        operations.append(op)
        groups["internal_holes"].append(op["id"])
    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] != "part":
            continue
        contour = _contour_operation(
            len(operations) + 1,
            entity,
            tool.id,
            cut_depth,
            inputs,
            request,
            warnings,
        )
        operations.append(contour)
        groups["contours"].append(contour["id"])
        if contour.get("tabs", {}).get("locations"):
            generated_entities.extend(_tab_entities(contour["entity"], contour["tabs"]["locations"]))

    if not operations:
        warnings.append(
            PlanningIssue(
                code="no_operations_generated",
                message="No machinable part or cutout entities were found in geom.yaml.",
            )
        )

    return {
        "schema_version": "1.0",
        "units": Units(length=geometry.units.length, speed=machine.units.speed).model_dump(mode="json"),
        "job": JobInfo(
            name="Generated Operation Plan",
            description="First-pass operation plan generated from geom.yaml",
            geometry_file=geometry.source.cleaned_file.replace("_fixed.dxf", "_geom.yaml"),
            machine="machine.yaml",
            post="post.yaml",
            planner="planner.yaml",
        ).model_dump(mode="json", exclude_none=True),
        "stock": Stock(
            material=material,
            thickness=stock_thickness,
            z_zero=inputs.z_zero_position or "stock_top",
            origin_location=request.machine.machine.coordinate_system.origin,
        ).model_dump(mode="json"),
        "coordinate_system": inputs.coordinate_system or "G54",
        "tools": _tool_summary(operations, request.machine),
        "generated_entities": generated_entities,
        "operation_groups": [
            {"name": name, "operations": op_ids}
            for name, op_ids in groups.items()
            if op_ids
        ],
        "operations": operations,
    }


def _selected_tool_ids(request: PlanningRequest) -> list[str]:
    value = request.inputs.tools
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _best_tool(request: PlanningRequest):
    machine_tools = {tool.id: tool for tool in request.machine.tools}
    selected_ids = [tool_id for tool_id in _selected_tool_ids(request) if tool_id in machine_tools]
    candidates = [machine_tools[tool_id] for tool_id in selected_ids] or list(machine_tools.values())
    return max(candidates, key=lambda tool: tool.diameter)


def _flatten_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten_nodes(node.get("children", [])))
    return result


def _pocket_operation(index: int, entity_id: str, tool_id: str, depth: float, direction: str) -> dict[str, Any]:
    return {
        "id": f"op{index}",
        "type": "pocket",
        "description": f"Pocket non-circular internal cutout {entity_id}",
        "entity": entity_id,
        "tool": tool_id,
        "depth": depth,
        "stepover_percent": 40,
        "strategy": "offset",
        "roughing": {
            "enabled": True,
            "depth_per_pass": 0.08,
            "side_allowance": 0.004,
            "bottom_allowance": 0.004,
            "milling_direction": direction,
        },
        "finishing": {
            "enabled": True,
            "side": True,
            "bottom": True,
            "passes": 1,
            "milling_direction": "climb",
        },
        "lead_in": {"type": "ramp", "length": 0.2},
    }


def _hole_operation(index: int, entity, tool_id: str, depth: float, direction: str) -> dict[str, Any]:
    diameter = entity.diameter or 0
    op_type = "drill" if diameter <= 0.2 else "helical_drill"
    operation = {
        "id": f"op{index}",
        "type": op_type,
        "description": f"{op_type.replace('_', ' ').title()} circular cutout {entity.id} ({diameter:.3f})",
        "entity": entity.id,
        "tool": tool_id,
        "depth": depth,
    }
    if op_type == "drill":
        operation.update({"peck_depth": min(depth, 0.12), "retract_amount": 0.04, "dwell_time": 0.2})
    else:
        operation.update(
            {
                "hole_diameter": diameter,
                "pitch": min(depth, 0.08),
                "milling_direction": direction,
        "finishing": {
            "enabled": True,
            "side": True,
            "bottom": False,
                    "passes": 1,
                    "milling_direction": direction,
                },
                "lead_in": {"type": "arc", "length": 0.2},
            }
        )
    return operation


def _contour_operation(
    index: int,
    entity,
    tool_id: str,
    depth: float,
    inputs: PlannerInputs,
    request: PlanningRequest,
    warnings: list[PlanningIssue],
) -> dict[str, Any]:
    entity_id = entity.id
    tabs_enabled = "tabs" in request.machine.machine.part_holding
    tab_height = 0.06 if (inputs.stock_material or "").lower() == "polycarbonate" else 0.1
    tab_width = max(inputs.stock_thickness or depth, 0.01)
    tab_locations = _tab_locations(entity, tab_width, tab_height, request) if tabs_enabled else []
    if tabs_enabled and len(tab_locations) < TAB_TARGET_COUNT:
        warnings.append(
            PlanningIssue(
                code="insufficient_tab_locations",
                entity=entity_id,
                message=(
                    f"Could only place {len(tab_locations)}/{TAB_TARGET_COUNT} tabs on {entity_id}; "
                    "the deterministic planner only places tabs on known straight line locations."
                ),
            )
        )
    return {
        "id": f"op{index}",
        "type": "contour",
        "description": f"Outer contour for part {entity_id}",
        "entity": entity_id,
        "offset": "outside",
        "tool": tool_id,
        "depth": depth,
        "extra_depth": _cut_deeper_than_stock(request),
        "roughing": {
            "enabled": True,
            "depth_per_pass": _tool_for_operation(request, {"tool": tool_id}).depth_per_pass or depth,
            "side_allowance": request.inputs.finishing_allowance,
            "bottom_allowance": 0.0,
            "milling_direction": "conventional",
        },
        "finishing": {
            "enabled": request.inputs.finishing_allowance > 0,
            "side": True,
            "bottom": False,
            "passes": 1,
            "milling_direction": "climb",
        },
        "tabs": {
            "enabled": tabs_enabled,
            "width": tab_width,
            "height": min(tab_height, depth),
            "count": len(tab_locations),
            "locations": tab_locations,
        },
        "lead_in": {"type": "ramp", "length": 0.25},
    }


def _cut_deeper_than_stock(request: PlanningRequest) -> float:
    return request.inputs.cut_deeper_than_stock


def _screw_entities(geometry: GeometryFile, diameter: float, spacing: float | None = None) -> list[dict[str, Any]]:
    bounds = _frame_or_summary_bounds(geometry)
    min_x, min_y, max_x, max_y = bounds
    inset = max(diameter * 2.5, 0.25)
    points: list[tuple[float, float]] = [
        (min_x + inset, min_y + inset),
        (max_x - inset, min_y + inset),
        (max_x - inset, max_y - inset),
        (min_x + inset, max_y - inset),
    ]
    if spacing:
        points.extend(_perimeter_screw_points(min_x, min_y, max_x, max_y, inset, spacing))
    points = _points_in_scrap(points, geometry)
    points = _unique_points(points)
    return [
        {
            "id": f"wh{index}",
            "role": "screw_hole",
            "shape": "circle",
            "center": {"x": x, "y": y},
            "diameter": diameter,
        }
        for index, (x, y) in enumerate(points, start=1)
    ]


def _perimeter_screw_points(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    inset: float,
    spacing: float,
) -> list[tuple[float, float]]:
    left = min_x + inset
    right = max_x - inset
    bottom = min_y + inset
    top = max_y - inset
    if right <= left or top <= bottom:
        return []
    return [
        *((x, bottom) for x in _interior_spacing_points(left, right, spacing)),
        *((x, top) for x in _interior_spacing_points(left, right, spacing)),
        *((left, y) for y in _interior_spacing_points(bottom, top, spacing)),
        *((right, y) for y in _interior_spacing_points(bottom, top, spacing)),
    ]


def _interior_spacing_points(start: float, end: float, spacing: float) -> list[float]:
    points = []
    value = start + spacing
    while value < end - spacing * 0.25:
        points.append(value)
        value += spacing
    return points


def _unique_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = []
    seen = set()
    for x, y in points:
        key = (round(x, 6), round(y, 6))
        if key in seen:
            continue
        seen.add(key)
        result.append((x, y))
    return result


def _points_in_scrap(points: list[tuple[float, float]], geometry: GeometryFile) -> list[tuple[float, float]]:
    if not any(node.role == "frame" for node in geometry.entity_map):
        return points
    part_boxes = _part_boxes(geometry)
    result = []
    for point in points:
        if any(_box_contains_point(box, point) for box in part_boxes):
            continue
        result.append(point)
    return result


def _part_boxes(geometry: GeometryFile):
    entities = {entity.id: entity for entity in geometry.entities}
    boxes = []
    for node in _flatten_nodes([node.model_dump() for node in geometry.entity_map]):
        if node["role"] != "part":
            continue
        entity = entities.get(node["entity"])
        if entity and entity.bounding_box:
            boxes.append(entity.bounding_box)
    return boxes


def _box_contains_point(box, point: tuple[float, float]) -> bool:
    x, y = point
    return box.min.x <= x <= box.max.x and box.min.y <= y <= box.max.y


def _screw_drill_operation(index: int, entity_id: str, tool_id: str, depth: float, clear_z: float, tool) -> dict[str, Any]:
    return {
        "id": f"op{index}",
        "type": "drill",
        "description": f"Pre-drill screw location {entity_id}",
        "entity": entity_id,
        "tool": tool_id,
        "depth": depth,
        "peck_depth": min(tool.depth_per_pass or tool.diameter, depth),
        "retract_amount": clear_z,
    }


def _frame_or_summary_bounds(geometry: GeometryFile) -> tuple[float, float, float, float]:
    entities = {entity.id: entity for entity in geometry.entities}
    for node in geometry.entity_map:
        if node.role == "frame":
            entity = entities.get(node.entity)
            if entity and entity.bounding_box:
                box = entity.bounding_box
                return box.min.x, box.min.y, box.max.x, box.max.y
    box = geometry.summary.bounding_box
    return box.min.x, box.min.y, box.max.x, box.max.y


def _tab_locations(entity, width: float, height: float, request: PlanningRequest) -> list[dict[str, Any]]:
    segment_tabs = _tab_locations_from_dxf(entity, width, height, request)
    if segment_tabs:
        return segment_tabs
    box = entity.bounding_box
    if box is None:
        return []
    if entity.shape not in {"rectangle", "polyline"}:
        return []
    min_x, min_y, max_x, max_y = box.min.x, box.min.y, box.max.x, box.max.y
    if max_x <= min_x or max_y <= min_y:
        return []
    horizontal = min(width, max_x - min_x)
    vertical = min(width, max_y - min_y)
    thickness = height
    return [
        _tab_rectangle((min_x + max_x) / 2, min_y, horizontal, thickness, 0.0),
        _tab_rectangle(max_x, (min_y + max_y) / 2, vertical, thickness, 90.0),
        _tab_rectangle((min_x + max_x) / 2, max_y, horizontal, thickness, 0.0),
        _tab_rectangle(min_x, (min_y + max_y) / 2, vertical, thickness, 90.0),
    ]


def _tab_locations_from_dxf(entity, width: float, height: float, request: PlanningRequest) -> list[dict[str, Any]]:
    if not request.fixed_dxf:
        return []
    dxf_entity = _dxf_entity_for_geometry_entity(entity, request.fixed_dxf)
    if dxf_entity is None or dxf_entity.dxftype() != "LWPOLYLINE":
        return []
    scale = request.geometry.units.coordinate_scale
    segments = _straight_polyline_segments(dxf_entity, scale)
    minimum_length = width * 1.05
    candidates = [segment for segment in segments if segment["length"] >= minimum_length]
    if not candidates:
        return []
    selected = _spread_segments(candidates, TAB_TARGET_COUNT)
    return [
        _tab_rectangle(
            segment["center"][0],
            segment["center"][1],
            width,
            height,
            segment["angle_deg"],
        )
        for segment in selected
    ]


def _dxf_entity_for_geometry_entity(entity, fixed_dxf: str):
    try:
        doc = ezdxf.read(StringIO(fixed_dxf))
    except Exception:
        logger.exception("Failed to read fixed DXF from planning request for tab placement")
        return None
    for ref in entity.source_refs:
        if ref.kind == "dxf_handle":
            return doc.entitydb.get(ref.value)
    return None


def _straight_polyline_segments(dxf_entity, scale: float) -> list[dict[str, Any]]:
    points = list(dxf_entity.get_points("xyseb"))
    if len(points) < 2:
        return []
    pairs = [(points[index], points[index + 1]) for index in range(len(points) - 1)]
    if dxf_entity.closed:
        pairs.append((points[-1], points[0]))
    segments = []
    for start, end in pairs:
        bulge = float(start[4])
        if abs(bulge) > 1e-6:
            continue
        x1, y1 = float(start[0]) * scale, float(start[1]) * scale
        x2, y2 = float(end[0]) * scale, float(end[1]) * scale
        dx = x2 - x1
        dy = y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length <= 1e-9:
            continue
        segments.append(
            {
                "center": ((x1 + x2) / 2, (y1 + y2) / 2),
                "length": length,
                "angle_deg": math.degrees(math.atan2(dy, dx)),
                "sort_angle": math.atan2((y1 + y2) / 2, (x1 + x2) / 2),
            }
        )
    return segments


def _spread_segments(segments: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    ordered = sorted(segments, key=lambda segment: segment["sort_angle"])
    if len(ordered) <= count:
        return ordered
    step = len(ordered) / count
    return [ordered[min(int(index * step), len(ordered) - 1)] for index in range(count)]


def _tab_rectangle(center_x: float, center_y: float, length: float, thickness: float, angle_deg: float) -> dict[str, Any]:
    radians = math.radians(angle_deg)
    ux, uy = math.cos(radians), math.sin(radians)
    nx, ny = -uy, ux
    half_length = length / 2
    half_thickness = thickness / 2
    corners = [
        (
            center_x + ux * sx * half_length + nx * sy * half_thickness,
            center_y + uy * sx * half_length + ny * sy * half_thickness,
        )
        for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return {
        "center": {"x": center_x, "y": center_y},
        "lower_left": {"x": min(xs), "y": min(ys)},
        "upper_right": {"x": max(xs), "y": max(ys)},
        "width": length,
        "height": thickness,
        "angle_deg": angle_deg,
    }


def _tab_entities(part_id: str, locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities = []
    for index, location in enumerate(locations, start=1):
        entities.append(
            {
                "id": f"wh_tab_{part_id}_{index}",
                "role": "tab",
                "shape": "rectangle",
                "center": location["center"],
                "lower_left": location["lower_left"],
                "upper_right": location["upper_right"],
                "width": location.get("width"),
                "height": location.get("height"),
                "angle_deg": location.get("angle_deg"),
            }
        )
    return entities


def _tool_summary(operations: list[dict[str, Any]], machine: MachineFile) -> list[dict[str, Any]]:
    tools = {tool.id: tool for tool in machine.tools}
    seen = set()
    result = []
    for operation in operations:
        tool_id = operation["tool"]
        if tool_id in seen or tool_id not in tools:
            continue
        seen.add(tool_id)
        result.append({"tool": tool_id, "diameter": tools[tool_id].diameter})
    return result


def _geometry_with_generated_entities(geometry: GeometryFile, plan: dict[str, Any]) -> dict[str, Any]:
    result = geometry.model_dump(mode="json", exclude_none=True)
    generated_entities = plan.get("generated_entities", [])
    result["generated_entities"] = generated_entities
    result["summary"]["generated_count"] = len(generated_entities)
    return result


def _milling_direction(material: str, flute_spiral: str) -> str:
    material = material.lower()
    if material in {"polycarbonate", "plastic", "plastics"} or flute_spiral == "compression":
        return "climb"
    return "climb"


def _dump_yaml(data: dict[str, Any]) -> str:
    return dump_operation_yaml(data)
