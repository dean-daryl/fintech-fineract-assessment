from fastapi import APIRouter, Request

from app.services.recovery import run_recovery

router = APIRouter(tags=["admin"])


@router.post("/admin/recover")
def recover(request: Request) -> dict[str, int]:
    return run_recovery(request.app.state.gl_sync, request.app.state.clients)
