from datetime import datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.gl_sync import GLSyncService

router = APIRouter(tags=["gl-accounts"])


class SyncGLAccountsIn(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)


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


@router.post("/gl-accounts/sync", status_code=status.HTTP_201_CREATED)
def sync_gl_accounts(body: SyncGLAccountsIn, request: Request) -> JSONResponse:
    result = _service(request).sync(body.external_reference)
    return JSONResponse(result.body, status_code=result.status_code)


@router.get("/gl-accounts", response_model=list[GLAccountSyncOut])
def list_gl_accounts(request: Request) -> list[GLAccountSyncOut]:
    return [GLAccountSyncOut.model_validate(row) for row in _service(request).list_statuses()]
