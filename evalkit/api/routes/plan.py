"""HTTP layer for the run planner."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..schemas import ErrorOut, EstimateIn, EstimateOut, PlanOptionsOut
from ..services.models_service import UnknownModelsError
from ..services.plan_service import PlanService, UnknownCategoriesError

router = APIRouter(prefix="/api/plan", tags=["plan"])


def get_service(request: Request) -> PlanService:
    return request.app.state.plan_service


@router.get("/options", response_model=PlanOptionsOut)
def options(service: PlanService = Depends(get_service)) -> PlanOptionsOut:
    return service.options()


@router.post("/estimate", response_model=EstimateOut, responses={422: {"model": ErrorOut}})
def estimate(body: EstimateIn, service: PlanService = Depends(get_service)) -> EstimateOut:
    """Pure computation: calls nothing, spends no quota. POST because the selection can be long."""
    try:
        return service.estimate(body)
    except (UnknownModelsError, UnknownCategoriesError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
