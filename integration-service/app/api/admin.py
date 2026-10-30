from fastapi import APIRouter, Request

from app.services.recovery import run_recovery

router = APIRouter(tags=["admin"])


@router.post("/admin/recover")
def recover(request: Request) -> dict[str, int]:
    state = request.app.state
    return run_recovery(state.gl_sync, state.pipeline, state.handlers)
