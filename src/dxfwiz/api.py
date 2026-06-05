import logging

from fastapi import APIRouter

from dxfwiz.planning import PlanningRequest, PlanningResponse, generate_operation_plan


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/plan", response_model=PlanningResponse)
def plan_operations(request: PlanningRequest) -> PlanningResponse:
    logger.info("Received operation planning request")
    return generate_operation_plan(request)
