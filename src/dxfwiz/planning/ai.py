from __future__ import annotations

import logging
from typing import Protocol

import instructor
import litellm
from pydantic import Field

from dxfwiz.config import GeminiConfig
from dxfwiz.planning.service import PlanningIssue, PlanningRequest, PlanningResponse
from dxfwiz.planning.yaml_format import dump_operation_yaml
from dxfwiz.schemas.common import StrictModel
from dxfwiz.schemas.job import JobFile


logger = logging.getLogger(__name__)


class PlannerClient(Protocol):
    def generate(self, request: PlanningRequest) -> PlanningResponse:
        ...


class PlanningAiResult(StrictModel):
    errors: list[PlanningIssue] = Field(default_factory=list)
    warnings: list[PlanningIssue] = Field(default_factory=list)
    plan: JobFile | None = None


class GeminiPlannerClient:
    def __init__(self, config: GeminiConfig) -> None:
        self.config = config

    def generate(self, request: PlanningRequest) -> PlanningResponse:
        if not self.config.api_key or self.config.api_key.startswith("REPLACE_"):
            return PlanningResponse(
                errors=[
                    PlanningIssue(
                        code="missing_gemini_api_key",
                        field="gemini.api_key",
                        message="Gemini API key is missing. Add it to config.yaml.",
                    )
                ],
                warnings=[],
                geometry=request.geometry.model_dump(mode="json", exclude_none=True),
                plan=None,
                op_yaml="",
            )

        logger.info("Calling Gemini operation planner model %s", self.config.model)
        client = instructor.from_litellm(litellm.completion, mode=instructor.Mode.JSON)
        try:
            result = client.chat.completions.create(
                response_model=PlanningAiResult,
                model=self.config.model,
                api_key=self.config.api_key,
                timeout=self.config.timeout_seconds,
                temperature=self.config.temperature,
                max_retries=self.config.max_retries,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert CNC router operation planner.",
                    },
                    {
                        "role": "user",
                        "content": _planner_prompt(request),
                    },
                ],
            )
        except Exception as exc:
            logger.warning("Gemini planning request failed: %s", exc)
            return PlanningResponse(
                errors=[
                    PlanningIssue(
                        code="ai_planner_error",
                        message=f"Gemini planning request failed: {exc}",
                    )
                ],
                geometry=request.geometry.model_dump(mode="json", exclude_none=True),
                plan=None,
                op_yaml="",
            )

        if result.errors:
            return PlanningResponse(
                errors=result.errors,
                warnings=result.warnings,
                geometry=request.geometry.model_dump(mode="json", exclude_none=True),
                plan=None,
                op_yaml="",
            )
        if result.plan is None:
            return PlanningResponse(
                errors=[
                    PlanningIssue(
                        code="missing_ai_plan",
                        message="Gemini did not return an operation plan.",
                    )
                ],
                warnings=result.warnings,
                geometry=request.geometry.model_dump(mode="json", exclude_none=True),
                plan=None,
                op_yaml="",
            )

        plan_data = result.plan.model_dump(mode="json", exclude_none=True)
        return PlanningResponse(
            errors=[],
            warnings=result.warnings,
            geometry=_geometry_with_generated_entities(request, plan_data),
            plan=plan_data,
            op_yaml=_dump_yaml(plan_data),
        )


def _planner_prompt(request: PlanningRequest) -> str:
    return "\n".join(
        [
            "Generate a complete op.yaml plan for a 2.5D CNC router job.",
            "Honor all system planning advice and user planner advice.",
            "Use the supplied Pydantic response schema exactly.",
            "If required information is missing or unsafe, return errors and leave plan null.",
            "The plan must match the op.yaml schema.",
            "Do not invent source DXF entities. If you add screws, tabs, or clamps, list them under generated_entities and reference those ids from operations.",
            "Use operation_groups for fixtures, internal_pockets, internal_holes, contours, and finish_contours when applicable.",
            "For through cuts, depth should be stock thickness plus cut_deeper_than_stock.",
            "For screw workholding, place screw holes in scrap areas when a stock frame and part geometry are present.",
            "",
            "GEOM_YAML:",
            _dump_yaml(request.geometry.model_dump(mode="json", exclude_none=True)),
            "",
            "MACHINE_YAML:",
            _dump_yaml(request.machine.model_dump(mode="json", exclude_none=True)),
            "",
            "SYSTEM_PLANNER_ADVICE_YAML:",
            _dump_yaml({"operation_advice": request.system_advice.model_dump(mode="json")}),
            "",
            "PLANNER_YAML:",
            _dump_yaml({"operation_advice": request.user_advice.model_dump(mode="json")}),
            "",
            "USER_PLANNING_INPUTS_YAML:",
            _dump_yaml(request.inputs.model_dump(mode="json", exclude_none=True)),
        ]
    )


def _geometry_with_generated_entities(request: PlanningRequest, plan: dict) -> dict:
    geometry = request.geometry.model_dump(mode="json", exclude_none=True)
    generated_entities = plan.get("generated_entities", [])
    geometry["generated_entities"] = generated_entities
    geometry["summary"]["generated_count"] = len(generated_entities)
    return geometry


def _dump_yaml(data: dict) -> str:
    return dump_operation_yaml(data)
