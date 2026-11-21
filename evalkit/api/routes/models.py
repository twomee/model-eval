"""HTTP layer for models: parse input, call the service, map errors to status codes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..schemas import DoctorIn, DoctorOut, ErrorOut, ModelsOut
from ..services.models_service import ModelsService, UnknownModelsError

router = APIRouter(prefix="/api", tags=["models"])


def get_service(request: Request) -> ModelsService:
    return request.app.state.models_service


@router.get("/models", response_model=ModelsOut)
def list_models(service: ModelsService = Depends(get_service)) -> ModelsOut:
    return service.list_models()


@router.post("/doctor", response_model=DoctorOut, responses={422: {"model": ErrorOut}})
async def doctor(body: DoctorIn, service: ModelsService = Depends(get_service)) -> DoctorOut:
    """Sends one real prompt per selected model/judge. Uses subscription quota, so POST."""
    try:
        return await service.doctor(body.names)
    except UnknownModelsError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
