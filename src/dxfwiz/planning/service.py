from __future__ import annotations

import logging
from importlib.resources import files
from typing import Any, Literal

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


class PlanningResponse(StrictModel):
    errors: list[PlanningIssue] = Field(default_factory=list)
    warnings: list[PlanningIssue] = Field(default_factory=list)
    geometry: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    op_yaml: str = ""


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
        job = JobFile.model_validate(response.plan)
        logger.info(
            "Generated AI operation plan with %d operation(s) and %d warning(s)",
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


class LocalPlannerClient:
    def generate(self, request: PlanningRequest) -> PlanningResponse:
        logger.info("Using local deterministic operation planner")
        plan = _build_plan(request, [])
        job = JobFile.model_validate(plan)
        plan_data = job.model_dump(mode="json", exclude_none=True)
        return PlanningResponse(
            errors=[],
            warnings=[],
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
        "finish_contours": [],
    }
    cut_depth = stock_thickness + _cut_deeper_than_stock(request)

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
        rough = _contour_operation(
            len(operations) + 1,
            entity,
            tool.id,
            cut_depth,
            "conventional",
            inputs,
            request,
            finish=False,
        )
        operations.append(rough)
        groups["contours"].append(rough["id"])
        if rough.get("tabs", {}).get("locations"):
            generated_entities.extend(_tab_entities(rough["entity"], rough["tabs"]["locations"]))
        finish = _contour_operation(
            len(operations) + 1,
            entity,
            tool.id,
            cut_depth,
            "climb",
            inputs,
            request,
            finish=True,
        )
        operations.append(finish)
        groups["finish_contours"].append(finish["id"])

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
        "milling_direction": direction,
        "finishing_pass": {"enabled": True, "allowance": 0.004},
        "lead_in": {"type": "line", "length": 0.2},
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
        operation.update({"milling_direction": direction, "stepover_percent": 35, "lead_in": {"type": "line", "length": 0.2}})
    return operation


def _contour_operation(
    index: int,
    entity,
    tool_id: str,
    depth: float,
    direction: str,
    inputs: PlannerInputs,
    request: PlanningRequest,
    finish: bool,
) -> dict[str, Any]:
    entity_id = entity.id
    tabs_enabled = "tabs" in request.machine.machine.part_holding and not finish
    tab_height = 0.06 if (inputs.stock_material or "").lower() == "polycarbonate" else 0.1
    finishing_allowance = 0.0 if finish else request.inputs.finishing_allowance
    tab_width = max(inputs.stock_thickness or depth, 0.01)
    tab_locations = _tab_locations(entity, tab_width, tab_height) if tabs_enabled else []
    return {
        "id": f"op{index}",
        "type": "contour",
        "description": f"{'Finish ' if finish else 'Rough '}outer contour for part {entity_id}",
        "entity": entity_id,
        "offset": "outside",
        "tool": tool_id,
        "depth": depth,
        "milling_direction": direction,
        "finishing_allowance": finishing_allowance,
        "finishing_pass": {"enabled": finish, "allowance": 0.0},
        "tabs": {
            "enabled": tabs_enabled,
            "width": tab_width,
            "height": min(tab_height, depth),
            "count": len(tab_locations) if tab_locations else 4,
            "locations": tab_locations,
        },
        "lead_in": {"type": "line", "length": 0.25},
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


def _tab_locations(entity, width: float, height: float) -> list[dict[str, Any]]:
    box = entity.bounding_box
    if box is None:
        return []
    min_x, min_y, max_x, max_y = box.min.x, box.min.y, box.max.x, box.max.y
    half_w = width / 2
    half_h = height / 2
    centers = [
        ((min_x + max_x) / 2, min_y),
        (max_x, (min_y + max_y) / 2),
        ((min_x + max_x) / 2, max_y),
        (min_x, (min_y + max_y) / 2),
    ]
    return [
        {
            "center": {"x": x, "y": y},
            "lower_left": {"x": x - half_w, "y": y - half_h},
            "upper_right": {"x": x + half_w, "y": y + half_h},
        }
        for x, y in centers
    ]


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
