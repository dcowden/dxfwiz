import logging

from fastapi import APIRouter

from dxfwiz.planning import PlanningRequest, PlanningResponse, generate_operation_plan
from dxfwiz.toolpaths import ToolpathRequest, ToolpathResponse, generate_toolpaths


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/plan", response_model=PlanningResponse)
def plan_operations(request: PlanningRequest) -> PlanningResponse:
    logger.info("Received operation planning request")
    return generate_operation_plan(request)


@router.post("/api/toolpaths", response_model=ToolpathResponse)
def post_toolpaths(request: ToolpathRequest) -> ToolpathResponse:
    logger.info("Received toolpath generation request")
    return generate_toolpaths(request)
