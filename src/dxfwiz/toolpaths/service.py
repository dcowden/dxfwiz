from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field

from dxfwiz.schemas import GeometryFile, JobFile, MachineFile
from dxfwiz.schemas.common import StrictModel
from dxfwiz.toolpaths.core import build_toolpath_program
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
    gcode: str = ""


def generate_toolpaths(request: ToolpathRequest) -> ToolpathResponse:
    logger.info("Generating toolpaths with %s post", request.post)
    try:
        program = build_toolpath_program(
            job=request.job,
            geometry=request.geometry,
            machine=request.machine,
            fixed_dxf=request.fixed_dxf,
        )
        post = UccncPost(precision=3 if request.job.units.length == "in" else 2)
        gcode = post.render(program)
    except Exception as exc:
        logger.exception("Toolpath generation failed")
        return ToolpathResponse(
            errors=[ToolpathIssue(code="toolpath_generation_failed", message=str(exc))],
            warnings=[],
            gcode="",
        )
    return ToolpathResponse(
        errors=[],
        warnings=[
            ToolpathIssue(code="toolpath_warning", message=message)
            for message in program.warnings
        ],
        gcode=gcode,
    )
