from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.faults import FaultInjector, FaultPoint
from app.services.recovery import run_recovery

router = APIRouter(tags=["admin"])


class OperationStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_type: str
    external_reference: str
    status: str
    attempts: int
    next_attempt_at: datetime
    locked_until: datetime | None
    last_error: str | None
    erp_transaction_id: str | None
    updated_at: datetime


class FaultIn(BaseModel):
    point: FaultPoint
    external_reference: str = Field(min_length=1, max_length=100)
    times: int = Field(default=1, ge=1)


@router.post("/admin/recover")
def recover(request: Request) -> dict[str, int]:
    state = request.app.state
    return run_recovery(state.gl_sync, state.pipeline, state.handlers)


@router.get("/admin/operations/{external_reference}", response_model=list[OperationStateOut])
def operations(external_reference: str, request: Request) -> list[Any]:
    return list(request.app.state.store.find_by_reference(external_reference))


def _faults(request: Request) -> FaultInjector:
    faults: FaultInjector = request.app.state.faults
    if not faults.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fault injection is disabled")
    return faults


@router.post("/admin/faults", status_code=status.HTTP_201_CREATED)
def arm_fault(body: FaultIn, request: Request) -> dict[str, Any]:
    _faults(request).arm(body.point, body.external_reference, body.times)
    return {"armed": _faults(request).armed()}


@router.get("/admin/faults")
def list_faults(request: Request) -> dict[str, Any]:
    return {"armed": _faults(request).armed()}


@router.delete("/admin/faults")
def clear_faults(request: Request) -> dict[str, Any]:
    _faults(request).clear()
    return {"armed": []}
