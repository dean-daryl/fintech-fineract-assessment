from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.clients.erp import ErpUnavailableError
from app.services.gl_sync import GLSyncService

router = APIRouter(tags=["gl-accounts"])


class GLAccountSyncOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    external_reference: str
    erp_code: str
    status: str
    attempts: int
    last_error: str | None
    updated_at: datetime


def _service(request: Request) -> GLSyncService:
    service: GLSyncService = request.app.state.gl_sync
    return service


@router.post("/gl-accounts/sync", response_model=list[GLAccountSyncOut])
def sync_gl_accounts(request: Request) -> list[GLAccountSyncOut]:
    try:
        rows = _service(request).sync_all()
    except ErpUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return [GLAccountSyncOut.model_validate(row) for row in rows]


@router.get("/gl-accounts", response_model=list[GLAccountSyncOut])
def list_gl_accounts(request: Request) -> list[GLAccountSyncOut]:
    return [GLAccountSyncOut.model_validate(row) for row in _service(request).list_statuses()]


@router.post("/admin/recover")
def recover(request: Request) -> dict[str, int]:
    return {"gl_accounts_processed": _service(request).recover()}
