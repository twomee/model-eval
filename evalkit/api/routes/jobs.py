"""HTTP layer for the Run button: start, poll, cancel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..schemas import CurrentJobOut, ErrorOut, EstimateIn, JobOut
from ..services.models_service import UnknownModelsError
from ..services.plan_service import UnknownCategoriesError
from ..services.run_service import JobConflictError, RunService

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def get_service(request: Request) -> RunService:
    return request.app.state.run_service


@router.post("", response_model=JobOut, status_code=202,
             responses={409: {"model": ErrorOut}, 422: {"model": ErrorOut}})
async def start(body: EstimateIn, service: RunService = Depends(get_service)) -> JobOut:
    """Starts a real run: spends subscription quota. 202 = accepted, poll /api/jobs/current."""
    try:
        return service.start(body).out()
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (UnknownModelsError, UnknownCategoriesError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/current", response_model=CurrentJobOut)
def current(service: RunService = Depends(get_service)) -> CurrentJobOut:
    return CurrentJobOut(job=service.current.out() if service.current else None)


@router.post("/current/cancel", response_model=JobOut, responses={409: {"model": ErrorOut}})
def cancel(service: RunService = Depends(get_service)) -> JobOut:
    try:
        return service.cancel().out()
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
