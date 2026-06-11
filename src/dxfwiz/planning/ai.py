from __future__ import annotations

import logging
import time
from typing import Protocol

import instructor
import litellm
from pydantic import Field

from dxfwiz.config import GeminiConfig
from dxfwiz.issues import issue_code
from dxfwiz.planning.ai_stats import AiCallStats, record_ai_call
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
                        code=issue_code("missing_gemini_api_key"),
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
        messages = [
            {
                "role": "system",
                "content": "You are an expert CNC router operation planner.",
            },
            {
                "role": "user",
                "content": _planner_prompt(request),
            },
        ]
        prompt_tokens = _count_tokens(self.config.model, messages=messages)
        start_time = time.perf_counter()
        try:
            result = client.chat.completions.create(
                response_model=PlanningAiResult,
                model=self.config.model,
                api_key=self.config.api_key,
                timeout=self.config.timeout_seconds,
                temperature=self.config.temperature,
                max_retries=self.config.max_retries,
                messages=messages,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start_time
            record_ai_call(
                AiCallStats(
                    model=self.config.model,
                    elapsed_seconds=elapsed,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    success=False,
                    error=str(exc),
                )
            )
            logger.warning("Gemini planning request failed: %s", exc)
            return PlanningResponse(
                errors=[
                    PlanningIssue(
                        code=issue_code("ai_planner_error"),
                        message=f"Gemini planning request failed: {exc}",
                    )
                ],
                geometry=request.geometry.model_dump(mode="json", exclude_none=True),
                plan=None,
                op_yaml="",
            )
        elapsed = time.perf_counter() - start_time
        completion_tokens = _count_tokens(self.config.model, text=result.model_dump_json())
        record_ai_call(
            AiCallStats(
                model=self.config.model,
                elapsed_seconds=elapsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                success=not bool(result.errors) and result.plan is not None,
                error="; ".join(error.message for error in result.errors) or None,
            )
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
                        code=issue_code("missing_ai_plan"),
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
    prompt = "\n".join(
        [
            "Generate a complete op.yaml plan for a 2.5D CNC router job.",
            "Honor all system planning advice and user planner advice.",
            "Use the supplied Pydantic response schema exactly.",
            "If required information is missing or unsafe, return errors and leave plan null.",
            "The plan must match the op.yaml schema.",
            "Hard planning rules:",
            "- Do not create operations for frame or ignored entities.",
            "- Use through-cut depth = stock_thickness + cut_deeper_than_stock.",
            "- Each operation references exactly one entity.",
            "- Do not create separate finish operations. Configure roughing and finishing inside the same operation.",
            "- If USER_PLANNING_INPUTS_YAML.finishing_allowance is greater than zero, every outside contour for a part entity needs one contour operation with roughing.side_allowance set to that value and finishing.enabled true.",
            "- Put outside contour operations in operation group contours.",
            "- Contour tabs belong only on contour operations, not pockets, drills, or helical drills.",
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
    return prompt


def _geometry_with_generated_entities(request: PlanningRequest, plan: dict) -> dict:
    geometry = request.geometry.model_dump(mode="json", exclude_none=True)
    generated_entities = plan.get("generated_entities", [])
    geometry["generated_entities"] = generated_entities
    geometry["summary"]["generated_count"] = len(generated_entities)
    return geometry


def _dump_yaml(data: dict) -> str:
    return dump_operation_yaml(data)


def _count_tokens(model: str, text: str | None = None, messages: list | None = None) -> int:
    try:
        return int(litellm.token_counter(model=model, text=text, messages=messages))
    except Exception:
        logger.debug("LiteLLM token counting failed", exc_info=True)
        if text is not None:
            return max(len(text) // 4, 1)
        if messages is not None:
            return max(sum(len(str(message.get("content", ""))) for message in messages) // 4, 1)
        return 0
