"""HTTP layer for results: run list, model comparison, the full HTML report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..schemas import ComparisonOut, ErrorOut, RunOut
from ..services.results_service import ResultsService, RunNotFoundError

router = APIRouter(tags=["results"])


def get_service(request: Request) -> ResultsService:
    return request.app.state.results_service


@router.get("/api/runs", response_model=list[RunOut])
def list_runs(service: ResultsService = Depends(get_service)) -> list[RunOut]:
    return service.list_runs()


@router.get("/api/runs/{run}/comparison", response_model=ComparisonOut, responses={404: {"model": ErrorOut}})
def comparison(run: str, service: ResultsService = Depends(get_service)) -> ComparisonOut:
    try:
        return service.comparison(run)
    except RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"run not found: {run}") from None


@router.get("/reports/{run}/report.html", include_in_schema=False)
def report(run: str, service: ResultsService = Depends(get_service)) -> FileResponse:
    try:
        return FileResponse(service.report_path(run), media_type="text/html")
    except RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"report not found: {run}") from None
