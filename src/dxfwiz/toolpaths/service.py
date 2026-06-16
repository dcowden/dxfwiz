from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field

from dxfwiz.issues import classify_toolpath_warning, issue_code
from dxfwiz.schemas import GeometryFile, JobFile, MachineFile
from dxfwiz.schemas.common import StrictModel
from dxfwiz.toolpaths.core import compile_toolpath_plan
from dxfwiz.toolpaths.model import ToolpathPlan
from dxfwiz.toolpaths.posts import UccncPost


logger = logging.getLogger(__name__)


class ToolpathIssue(StrictModel):
    code: str
    message: str
    operation: str | None = None


class ToolpathRequest(StrictModel):
    job: JobFile
    geometry: GeometryFile
    machine: MachineFile
    post: Literal["uccnc"] = "uccnc"
    fixed_dxf: str | None = None


class ToolpathResponse(StrictModel):
    errors: list[ToolpathIssue] = Field(default_factory=list)
    warnings: list[ToolpathIssue] = Field(default_factory=list)
    plan: ToolpathPlan | None = None
    gcode: str = ""
    gcode_files: dict[str, str] = Field(default_factory=dict)


def generate_toolpaths(request: ToolpathRequest) -> ToolpathResponse:
    logger.info("Generating toolpaths with %s post", request.post)
    try:
        plan = compile_toolpath_plan(
            job=request.job,
            geometry=request.geometry,
            machine=request.machine,
            fixed_dxf=request.fixed_dxf,
        )
        post = UccncPost(precision=3 if request.job.units.length == "in" else 2)
        gcode = post.render(plan)
        gcode_files = _split_gcode_files_by_tool(request, plan, post)
    except Exception as exc:
        logger.exception("Toolpath generation failed")
        return ToolpathResponse(
            errors=[ToolpathIssue(code=issue_code("toolpath_generation_failed"), message=str(exc))],
            warnings=[],
            gcode="",
            gcode_files={},
        )
    issues = [
        ToolpathIssue(code=classify_toolpath_warning(message), message=message)
        for message in plan.warnings
    ]
    issues.extend(_gcode_validation_issues(gcode, plan, request.machine.machine.clear_z))
    return ToolpathResponse(
        errors=[issue for issue in issues if issue.code.startswith("E")],
        warnings=[issue for issue in issues if issue.code.startswith("W")],
        plan=plan,
        gcode=gcode,
        gcode_files=gcode_files,
    )


def _gcode_validation_issues(gcode: str, plan: ToolpathPlan, safe_z: float) -> list[ToolpathIssue]:
    from dxfwiz.gcode.validation import validate_gcode_against_plan

    result: list[ToolpathIssue] = []
    for issue in validate_gcode_against_plan(gcode, plan, safe_z=safe_z, tolerance=0.002):
        code = issue_code("gcode_validation_failed") if issue.severity == "error" else "W1002"
        location = f"line {issue.line_no}: " if issue.line_no is not None else ""
        result.append(ToolpathIssue(code=code, message=f"{location}{issue.message}"))
    return result


def _split_gcode_files_by_tool(request: ToolpathRequest, plan: ToolpathPlan, post: UccncPost) -> dict[str, str]:
    if not request.machine.machine.separate_nc_file_per_tool:
        return {}
    tools = _tools_used_in_plan(plan)
    if len(tools) <= 1:
        return {}
    result: dict[str, str] = {}
    for tool_id in tools:
        tool_plan = _plan_for_tool(plan, tool_id)
        result[f"{_safe_job_name(request.job.job.name)}_{tool_id}.nc"] = post.render(tool_plan)
    return result


def _tools_used_in_plan(plan: ToolpathPlan) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for toolpath_pass in plan.passes:
        if not toolpath_pass.tool or toolpath_pass.tool in seen:
            continue
        seen.add(toolpath_pass.tool)
        result.append(toolpath_pass.tool)
    return result


def _plan_for_tool(plan: ToolpathPlan, tool_id: str) -> ToolpathPlan:
    passes = [toolpath_pass for toolpath_pass in plan.passes if toolpath_pass.tool == tool_id]
    if passes:
        last = passes[-1]
        moves = [*last.moves]
        if not any(getattr(move, "type", None) == "program_end" for move in moves):
            moves.extend([{"type": "spindle", "state": "off"}, {"type": "program_end"}])
            data = last.model_dump(mode="json", exclude_none=True)
            data["moves"] = moves
            passes[-1] = type(last).model_validate(data)
    return ToolpathPlan(
        units=plan.units,
        coordinate_system=plan.coordinate_system,
        commands=plan.commands,
        source_paths=plan.source_paths,
        passes=passes,
        warnings=plan.warnings,
    )


def _safe_job_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name.strip())
    return safe.strip("_") or "job"
