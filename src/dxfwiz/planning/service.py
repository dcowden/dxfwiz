from __future__ import annotations

import logging
import math
from io import StringIO
from importlib.resources import files
from typing import Any, Literal

import ezdxf
from ezdxf import path as ezdxf_path
from pydantic import Field
from ruamel.yaml import YAML
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from dxfwiz.config import load_config
from dxfwiz.issues import issue_code
from dxfwiz.schemas import GeometryFile, JobFile, MachineFile, PlannerFile
from dxfwiz.schemas.common import StrictModel, Units
from dxfwiz.schemas.job import JobInfo, Stock
from dxfwiz.schemas.machine import ScrewWorkholdingMethod, Tool
from dxfwiz.schemas.planner import OperationAdvice, OperationSettings
from dxfwiz.planning.yaml_format import dump_operation_yaml
from dxfwiz.planning.tool_selector import select_largest_single_tool, selected_tool_rejections


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
    ideal_screw_distance: float | None = Field(default=None, gt=0)
    min_screw_distance: float | None = Field(default=None, gt=0)
    operation_settings: OperationSettings = Field(default_factory=OperationSettings)
    fixups: dict[str, bool] = Field(default_factory=dict)


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
    if response.errors and _fixup_enabled(request, "fall_back_to_local_planner"):
        fallback_response = LocalPlannerClient().generate(request)
        fallback_response.warnings = [
            *warnings,
            PlanningIssue(
                code=issue_code("ai_planner_fell_back_to_local"),
                message="Planner errors were returned; used deterministic local planner as a configured fixup.",
            ),
            *fallback_response.warnings,
        ]
        return fallback_response
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
    if not _fixup_enabled(request, "normalize_contour_finishing"):
        return []
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
                code=issue_code("separate_finish_contours_merged"),
                message=(
                    f"Merged {removed_finish_count} separate finish contour operation(s) into their parent "
                    "contour operation finishing settings."
                ),
            )
        )
    if updated_finish_count and request.inputs.finishing_allowance > 0:
        warnings.append(
            PlanningIssue(
                code=issue_code("contour_finishing_settings_repaired"),
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
        operation["ramping"] = True
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
    if operation["type"] == "helical_contour":
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
    if operation["type"] == "helical_pocket":
        operation.setdefault("pitch", depth_per_pass)
        operation.setdefault("stepover_percent", request.inputs.operation_settings.pocket_stepover_percent)
        operation.setdefault("prefer_arcs", request.inputs.operation_settings.prefer_arcs)
        operation.setdefault("milling_direction", milling_direction)
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
            "enabled": bool(operation.get("finishing", {}).get("enabled", True)),
            "side": True,
            "bottom": True,
            "passes": operation.get("finishing", {}).get("passes", 1),
            "milling_direction": operation.get("finishing", {}).get("milling_direction") or "climb",
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
            errors.append(PlanningIssue(code=issue_code("missing_required_input"), field=field, message=message))
    return errors


def _planning_warnings(request: PlanningRequest) -> list[PlanningIssue]:
    warnings: list[PlanningIssue] = []
    selected_tools = _selected_tool_ids(request)
    machine_tools = {tool.id: tool for tool in request.machine.tools}
    missing_tools = [tool_id for tool_id in selected_tools if tool_id not in machine_tools]
    for tool_id in missing_tools:
        warnings.append(
            PlanningIssue(
                code=issue_code("unknown_tool"),
                field="tools",
                message=f"Selected tool {tool_id} is not in machine.yaml and will be ignored.",
            )
        )
    usable_tool_count = len([tool_id for tool_id in selected_tools if tool_id in machine_tools])
    for tool_id in [tool_id for tool_id in selected_tools if tool_id in machine_tools]:
        tool = machine_tools[tool_id]
        rejections = selected_tool_rejections(tool, request.geometry, request.fixed_dxf)
        if rejections and _fixup_enabled(request, "replace_invalid_default_tool"):
            replacement = select_largest_single_tool(request.geometry, request.machine, request.fixed_dxf).tool
            warnings.append(
                PlanningIssue(
                    code=issue_code("invalid_default_tool_replaced"),
                    field="tools",
                    message=(
                        f"Requested tool {tool.id} cannot machine required features; "
                        f"using {replacement.id} ({replacement.diameter:.4f}) instead."
                    ),
                )
            )
    if usable_tool_count > request.machine.machine.max_tools:
        warnings.append(
            PlanningIssue(
                code=issue_code("tool_change_required"),
                field="tools",
                message="Selected tools exceed machine max_tools; manual tool changes or replanning are required.",
            )
        )
    if "tabs" in request.machine.machine.part_holding:
        if _fixup_enabled(request, "accept_reduced_tab_count"):
            warnings.append(
                PlanningIssue(
                    code=issue_code("tabs_not_fully_placed"),
                    message="Tab placement is advisory in this first planner pass; verify straight segments before cutting.",
                )
            )
    if request.inputs.planning_notes:
        warnings.append(
            PlanningIssue(
                code=issue_code("notes_not_interpreted"),
                field="planning_notes",
                message="Additional instructions are preserved for review but not fully interpreted by the first-pass planner.",
            )
        )
    close_part_warning = _close_part_warning(request)
    if close_part_warning is not None:
        warnings.append(close_part_warning)
    return warnings


def _fixup_enabled(request: PlanningRequest, name: str) -> bool:
    defaults = {
        "replace_invalid_default_tool": True,
        "normalize_contour_finishing": True,
        "accept_reduced_tab_count": True,
        "skip_roughing_when_finish_fits": True,
        "fall_back_to_local_planner": False,
        "downgrade_pocket_to_profile_when_tool_fits_boundary": False,
    }
    return request.inputs.fixups.get(name, defaults.get(name, False))


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
        code=issue_code("parts_too_close"),
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
        screw_tool = _screw_tool(request, tool)
        screw_hole_diameter = _planned_screw_hole_diameter(request, screw_tool)
        for entity in _screw_entities(request, screw_hole_diameter):
            generated_entities.append(entity)
            op = _screw_drill_operation(
                len(operations) + 1,
                entity["id"],
                screw_tool.id,
                stock_thickness + _cut_deeper_than_stock(request),
                machine.machine.clear_z,
                screw_tool,
            )
            operations.append(op)
            groups["fixtures"].append(op["id"])

    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] in {"frame", "ignored", "uncontained"}:
            continue
        if node["role"] == "cutout" and entity.shape != "circle":
            op = _pocket_operation(
                len(operations) + 1,
                entity.id,
                tool.id,
                cut_depth,
                milling_direction,
                inputs.operation_settings,
            )
            operations.append(op)
            groups["internal_pockets"].append(op["id"])
    for node in nodes:
        entity = entities.get(node["entity"])
        if entity is None or node["role"] != "cutout" or entity.shape != "circle":
            continue
        op = _hole_operation(len(operations) + 1, entity, tool.id, cut_depth, milling_direction, request)
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
                code=issue_code("no_operations_generated"),
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
    if selected_ids:
        selected = max((machine_tools[tool_id] for tool_id in selected_ids), key=lambda tool: tool.diameter)
        rejections = selected_tool_rejections(selected, request.geometry, request.fixed_dxf)
        if not rejections:
            return selected
        if not _fixup_enabled(request, "replace_invalid_default_tool"):
            return selected
        logger.info("Selected tool %s does not fit geometry; falling back to largest fitting tool", selected.id)
    return select_largest_single_tool(request.geometry, request.machine, request.fixed_dxf).tool


def _flatten_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten_nodes(node.get("children", [])))
    return result


def _pocket_operation(
    index: int,
    entity_id: str,
    tool_id: str,
    depth: float,
    direction: str,
    settings: OperationSettings,
) -> dict[str, Any]:
    return {
        "id": f"op{index}",
        "type": "pocket",
        "description": f"Pocket non-circular internal cutout {entity_id}",
        "entity": entity_id,
        "tool": tool_id,
        "depth": depth,
        "stepover_percent": settings.pocket_stepover_percent,
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


def _hole_operation(
    index: int,
    entity,
    tool_id: str,
    depth: float,
    direction: str,
    request: PlanningRequest,
) -> dict[str, Any]:
    diameter = entity.diameter or 0
    settings = request.inputs.operation_settings
    tool = next((tool for tool in request.machine.tools if tool.id == tool_id), None)
    tool_diameter = tool.diameter if tool is not None else 0.0
    max_plug = request.machine.machine.maximum_plug_size
    helical_max = settings.helical_pocket_max_diameter or settings.helical_drill_max_diameter
    op_type = _hole_operation_type(diameter, tool_diameter, settings.drill_max_diameter, max_plug, helical_max)
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
    elif op_type == "helical_contour":
        operation.update(
            {
                "hole_diameter": diameter,
                "pitch": min(depth, 0.08),
                "milling_direction": direction,
                "skip_roughing_when_finish_fits": _fixup_enabled(request, "skip_roughing_when_finish_fits"),
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
    elif op_type == "helical_pocket":
        operation.update(
            {
                "hole_diameter": diameter,
                "pitch": min(depth, 0.08),
                "stepover_percent": settings.pocket_stepover_percent,
                "prefer_arcs": settings.prefer_arcs,
                "milling_direction": direction,
                "roughing": {
                    "enabled": True,
                    "depth_per_pass": 0.08,
                    "side_allowance": request.inputs.finishing_allowance,
                    "bottom_allowance": request.inputs.finishing_allowance,
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
        )
    else:
        operation.update(
            {
                "type": "pocket",
                "description": f"Pocket large circular cutout {entity.id} ({diameter:.3f})",
                "strategy": "offset",
                "stepover_percent": settings.pocket_stepover_percent,
                "roughing": {
                    "enabled": True,
                    "depth_per_pass": 0.08,
                    "side_allowance": request.inputs.finishing_allowance,
                    "bottom_allowance": request.inputs.finishing_allowance,
                    "milling_direction": direction,
                },
                "finishing": {
                    "enabled": True,
                    "side": True,
                    "bottom": True,
                    "passes": 1,
                    "milling_direction": direction,
                },
                "lead_in": {"type": "ramp", "length": 0.2},
            }
        )
    return operation


def _hole_operation_type(
    hole_diameter: float,
    tool_diameter: float,
    drill_max_diameter: float,
    max_plug_diameter: float,
    helical_pocket_max_diameter: float,
) -> str:
    if hole_diameter <= drill_max_diameter or math.isclose(hole_diameter, tool_diameter, abs_tol=1e-4):
        return "drill"
    plug_diameter = max(0.0, hole_diameter - (2 * tool_diameter))
    if plug_diameter <= max_plug_diameter:
        return "helical_contour"
    if hole_diameter <= helical_pocket_max_diameter:
        return "helical_pocket"
    return "pocket"


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
                code=issue_code("insufficient_tab_locations"),
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
        "ramping": True,
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


def _screw_entities(request: PlanningRequest, diameter: float) -> list[dict[str, Any]]:
    points = _screw_points(request)
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


def _screw_points(request: PlanningRequest) -> list[tuple[float, float]]:
    geometry = request.geometry
    bounds = _frame_or_summary_bounds(geometry)
    min_x, min_y, max_x, max_y = bounds
    screw_workholding = _screw_workholding(request.machine)
    method_grid = screw_workholding.screw_grid if screw_workholding else None
    grid = method_grid or request.machine.machine.screw_grid or request.inputs.screw_spacing
    if not grid:
        logger.info("Skipping screw placement because no screw_grid is configured")
        return []
    clearance = (
        (screw_workholding.screw_clearance if screw_workholding else None)
        or request.machine.machine.screw_clearance
        or 0.0
    )
    stock = box(min_x, min_y, max_x, max_y)
    blocked = _part_clearance_polygons(request, clearance)
    offset = (
        screw_workholding.screw_grid_offset
        if screw_workholding is not None and method_grid is not None
        else request.machine.machine.screw_grid_offset
    )
    candidates = [
        point
        for point in _grid_points(min_x, min_y, max_x, max_y, grid, (offset.x, offset.y))
        if _point_in_stock_scrap(point, stock, blocked)
    ]
    candidates = _unique_points(candidates)
    ideal_spacing = _ideal_screw_distance(request)
    selected = _select_screw_points_from_grid(candidates, stock, bounds, ideal_spacing, grid)
    return _unique_points(selected)


def _screw_workholding(machine: MachineFile) -> ScrewWorkholdingMethod | None:
    for method in machine.machine.workholding.supported_methods:
        if isinstance(method, ScrewWorkholdingMethod):
            return method
    return None


def _screw_tool(request: PlanningRequest, selected_tool: Tool) -> Tool:
    screw_workholding = _screw_workholding(request.machine)
    if screw_workholding is None or screw_workholding.allow_oversized_holes_to_prevent_toolchange:
        return selected_tool
    screw_diameter = screw_workholding.screw_hole_diameter
    fitting_tools = [tool for tool in request.machine.tools if tool.diameter <= screw_diameter + 1e-9]
    if fitting_tools:
        return max(fitting_tools, key=lambda tool: tool.diameter)
    return selected_tool


def _planned_screw_hole_diameter(request: PlanningRequest, screw_tool: Tool) -> float:
    screw_workholding = _screw_workholding(request.machine)
    target = screw_workholding.screw_hole_diameter if screw_workholding else screw_tool.diameter
    if screw_workholding is None or screw_workholding.allow_oversized_holes_to_prevent_toolchange:
        return max(target, screw_tool.diameter)
    return target


def _default_screw_spacing(request: PlanningRequest) -> float:
    return 12.0 if request.geometry.units.length == "in" else 300.0


def _ideal_screw_distance(request: PlanningRequest) -> float:
    return (
        request.inputs.ideal_screw_distance
        or request.inputs.screw_spacing
        or request.inputs.min_screw_distance
        or _default_screw_spacing(request)
    )


def _grid_points(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    spacing: float,
    offset: tuple[float, float] = (0.0, 0.0),
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    x_offset, y_offset = offset
    x = x_offset + math.floor((min_x - x_offset) / spacing) * spacing
    while x <= max_x + spacing + 1e-9:
        y = y_offset + math.floor((min_y - y_offset) / spacing) * spacing
        while y <= max_y + spacing + 1e-9:
            points.append((round(x, 6), round(y, 6)))
            y += spacing
        x += spacing
    return points


def _point_in_stock_scrap(point: tuple[float, float], stock: Polygon, blocked) -> bool:
    location = Point(point)
    if not stock.covers(location):
        return False
    if blocked is None or blocked.is_empty:
        return True
    return not blocked.covers(location)


def _select_screw_points_from_grid(
    candidates: list[tuple[float, float]],
    stock: Polygon,
    bounds: tuple[float, float, float, float],
    ideal_spacing: float,
    screw_grid: float,
) -> list[tuple[float, float]]:
    if not candidates:
        return []
    min_x, min_y, max_x, max_y = bounds
    candidate_set = set(candidates)
    selected = _corner_screw_points(candidates, bounds, screw_grid)
    coverage_radius = ideal_spacing / 2
    uncovered = set(_coverage_sample_points(stock, bounds, coverage_radius))
    if not uncovered:
        return selected
    uncovered = _remove_covered(uncovered, selected, coverage_radius)
    max_extra = max(0, min(len(candidates), math.ceil(stock.area / max(ideal_spacing * ideal_spacing, 1e-9)) * 4 + 8))
    center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
    while uncovered and len(selected) < max_extra:
        existing = set(selected)
        best = max(
            (point for point in candidate_set if point not in existing),
            key=lambda point: _screw_candidate_score(point, uncovered, selected, center, bounds, coverage_radius),
            default=None,
        )
        if best is None:
            break
        covered_by_best = _covered_points(uncovered, best, coverage_radius)
        if not covered_by_best:
            break
        selected.append(best)
        uncovered.difference_update(covered_by_best)
    return _unique_points(selected)


def _corner_screw_points(
    candidates: list[tuple[float, float]],
    bounds: tuple[float, float, float, float],
    screw_grid: float,
) -> list[tuple[float, float]]:
    min_x, min_y, max_x, max_y = bounds
    corners = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
    selected = []
    for corner in corners:
        point = min(candidates, key=lambda candidate: _distance(candidate, corner))
        if _distance(point, corner) <= screw_grid * 1.5:
            selected.append(point)
    return _unique_points(selected)


def _coverage_sample_points(
    stock: Polygon,
    bounds: tuple[float, float, float, float],
    ideal_spacing: float,
) -> list[tuple[float, float]]:
    min_x, min_y, max_x, max_y = bounds
    sample_spacing = max(ideal_spacing / 4, min(max_x - min_x, max_y - min_y, ideal_spacing) / 12, 1e-6)
    points: list[tuple[float, float]] = []
    x = min_x
    while x <= max_x + 1e-9:
        y = min_y
        while y <= max_y + 1e-9:
            point = (round(min(x, max_x), 6), round(min(y, max_y), 6))
            if stock.covers(Point(point)):
                points.append(point)
            y += sample_spacing
        x += sample_spacing
    points.extend([(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y), ((min_x + max_x) / 2, (min_y + max_y) / 2)])
    return _unique_points(points)


def _remove_covered(
    sample_points: set[tuple[float, float]],
    selected: list[tuple[float, float]],
    radius: float,
) -> set[tuple[float, float]]:
    result = set(sample_points)
    for point in selected:
        result.difference_update(_covered_points(result, point, radius))
    return result


def _covered_points(
    sample_points: set[tuple[float, float]],
    candidate: tuple[float, float],
    radius: float,
) -> set[tuple[float, float]]:
    radius_sq = radius * radius
    return {point for point in sample_points if _distance_sq(point, candidate) <= radius_sq + 1e-9}


def _screw_candidate_score(
    candidate: tuple[float, float],
    uncovered: set[tuple[float, float]],
    selected: list[tuple[float, float]],
    center: tuple[float, float],
    bounds: tuple[float, float, float, float],
    coverage_radius: float,
) -> float:
    covered = _covered_points(uncovered, candidate, coverage_radius)
    if not covered:
        return -1e9
    min_x, min_y, max_x, max_y = bounds
    diagonal = max(_distance((min_x, min_y), (max_x, max_y)), 1e-9)
    nearest_selected = min((_distance(candidate, point) for point in selected), default=coverage_radius)
    spacing_factor = min(nearest_selected / max(coverage_radius, 1e-9), 1.0)
    centrality = 1.0 - min(_distance(candidate, center) / diagonal, 1.0)
    edge_distance = min(candidate[0] - min_x, max_x - candidate[0], candidate[1] - min_y, max_y - candidate[1])
    edge_factor = min(max(edge_distance, 0.0) / max(coverage_radius / 2, 1e-9), 1.0)
    return (
        len(covered) * 1000
        + spacing_factor * 30
        + centrality * 10
        + edge_factor * 5
        - max(0.0, (coverage_radius * 0.4) - nearest_selected) * 20
    )


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _distance_sq(first: tuple[float, float], second: tuple[float, float]) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _part_clearance_polygons(request: PlanningRequest, clearance: float):
    polygons = [
        polygon.buffer(clearance, join_style=2)
        for polygon in _part_polygons(request)
        if not polygon.is_empty
    ]
    if not polygons:
        return None
    return unary_union(polygons)


def _part_polygons(request: PlanningRequest) -> list[Polygon]:
    entities = {entity.id: entity for entity in request.geometry.entities}
    polygons: list[Polygon] = []
    for node in _flatten_nodes([node.model_dump() for node in request.geometry.entity_map]):
        if node["role"] != "part":
            continue
        entity = entities.get(node["entity"])
        if entity is None:
            continue
        polygon = _entity_polygon(entity, request)
        if polygon is not None and not polygon.is_empty:
            polygons.append(polygon)
    return polygons


def _entity_polygon(entity, request: PlanningRequest) -> Polygon | None:
    if request.fixed_dxf:
        dxf_entity = _dxf_entity_for_geometry_entity(entity, request.fixed_dxf)
        polygon = _dxf_polygon(dxf_entity, request.geometry.units.coordinate_scale)
        if polygon is not None:
            return polygon
    if entity.shape == "circle" and entity.center and entity.diameter:
        return Point(entity.center.x, entity.center.y).buffer(entity.diameter / 2, quad_segs=48)
    if entity.bounding_box:
        bounds = entity.bounding_box
        return box(bounds.min.x, bounds.min.y, bounds.max.x, bounds.max.y)
    return None


def _dxf_polygon(dxf_entity, scale: float) -> Polygon | None:
    if dxf_entity is None:
        return None
    if dxf_entity.dxftype() == "CIRCLE":
        center = dxf_entity.dxf.center
        radius = float(dxf_entity.dxf.radius) * scale
        return Point(float(center.x) * scale, float(center.y) * scale).buffer(radius, quad_segs=48)
    try:
        points = [
            (float(vertex.x) * scale, float(vertex.y) * scale)
            for vertex in ezdxf_path.make_path(dxf_entity).flattening(0.005 / max(scale, 1e-9))
        ]
    except Exception:
        logger.debug("Could not flatten DXF entity %s for screw clearance polygon", dxf_entity, exc_info=True)
        return None
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon if not polygon.is_empty else None


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
