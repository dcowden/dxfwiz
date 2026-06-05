from __future__ import annotations

import logging
from importlib.resources import files
from typing import Any, Literal

from pydantic import Field
from ruamel.yaml import YAML

from dxfwiz.schemas import GeometryFile, JobFile, MachineFile, PlannerFile
from dxfwiz.schemas.common import StrictModel, Units
from dxfwiz.schemas.job import JobInfo, Stock
from dxfwiz.schemas.planner import OperationAdvice


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


class PlanningRequest(StrictModel):
    geometry: GeometryFile
    machine: MachineFile
    system_advice: OperationAdvice
    user_advice: OperationAdvice
    inputs: PlannerInputs


class PlanningResponse(StrictModel):
    errors: list[PlanningIssue] = Field(default_factory=list)
    warnings: list[PlanningIssue] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    op_yaml: str = ""


def load_system_planner_advice() -> OperationAdvice:
    advice_path = files("dxfwiz.planning").joinpath("system_planner_advice.yaml")
    logger.debug("Loading system planner advice from %s", advice_path)
    data = YAML(typ="safe").load(advice_path.read_text(encoding="utf-8"))
    return OperationAdvice.model_validate(data["operation_advice"])


def generate_operation_plan(request: PlanningRequest) -> PlanningResponse:
    logger.info("Generating operation plan")
    errors = _validate_required_inputs(request.inputs)
    warnings = _planning_warnings(request)
    if errors:
        logger.info("Operation plan has %d blocking input error(s)", len(errors))
        return PlanningResponse(errors=errors, warnings=warnings, plan=None, op_yaml="")

    plan = _build_plan(request, warnings)
    job = JobFile.model_validate(plan)
    logger.info(
        "Generated operation plan with %d operation(s) and %d warning(s)",
        len(job.operations),
        len(warnings),
    )
    return PlanningResponse(
        errors=[],
        warnings=warnings,
        plan=job.model_dump(mode="json", exclude_none=True),
        op_yaml=_dump_yaml(job.model_dump(mode="json", exclude_none=True)),
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
    return warnings


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

    if "screws" in inputs.workholding_method:
        operations.append(
            {
                "id": "op1",
                "type": "drill",
                "description": "Pre-drill screw locations in stock corners and scrap areas",
                "entity": _first_frame_or_part(nodes),
                "tool": tool.id,
                "depth": min(stock_thickness, tool.depth_per_pass or stock_thickness),
                "peck_depth": min(tool.depth_per_pass or tool.diameter, stock_thickness),
                "retract_amount": machine.machine.clear_z,
            }
        )

    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] in {"frame", "ignored", "uncontained"}:
            continue
        if node["role"] == "cutout" and entity.shape != "circle":
            operations.append(_pocket_operation(len(operations) + 1, entity.id, tool.id, stock_thickness, milling_direction))
    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] != "cutout" or entity.shape != "circle":
            continue
        operations.append(_hole_operation(len(operations) + 1, entity, tool.id, stock_thickness, milling_direction))
    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] != "part":
            continue
        operations.append(_contour_operation(len(operations) + 1, entity.id, tool.id, stock_thickness, milling_direction, inputs, request))

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


def _first_frame_or_part(nodes: list[dict[str, Any]]) -> str:
    for role in ("frame", "part"):
        for node in nodes:
            if node["role"] == role:
                return node["entity"]
    return nodes[0]["entity"] if nodes else "e1"


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
    entity_id: str,
    tool_id: str,
    depth: float,
    direction: str,
    inputs: PlannerInputs,
    request: PlanningRequest,
) -> dict[str, Any]:
    tabs_enabled = "tabs" in request.machine.machine.part_holding
    tab_height = 0.06 if (inputs.stock_material or "").lower() == "polycarbonate" else 0.1
    return {
        "id": f"op{index}",
        "type": "contour",
        "description": f"Outer contour for part {entity_id}",
        "entity": entity_id,
        "offset": "outside",
        "tool": tool_id,
        "depth": depth,
        "milling_direction": direction,
        "finishing_allowance": 0.0,
        "tabs": {
            "enabled": tabs_enabled,
            "width": max(depth, 0.01),
            "height": min(tab_height, depth),
            "count": 4,
        },
        "lead_in": {"type": "line", "length": 0.25},
    }


def _milling_direction(material: str, flute_spiral: str) -> str:
    material = material.lower()
    if material in {"polycarbonate", "plastic", "plastics"} or flute_spiral == "compression":
        return "climb"
    return "climb"


def _dump_yaml(data: dict[str, Any]) -> str:
    from io import StringIO

    buffer = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 120
    yaml.dump(data, buffer)
    return buffer.getvalue()
