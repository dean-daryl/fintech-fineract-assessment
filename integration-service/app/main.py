import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.admin import router as admin_router
from app.api.clients import router as clients_router
from app.api.gl_accounts import router as gl_accounts_router
from app.api.loans import router as loans_router
from app.api.reconciliation import router as reconciliation_router
from app.auth import require_admin_key, require_partner_key
from app.clients.erp import ErpClient
from app.clients.fineract import FineractClient
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.errors import INVALID_REQUEST, error_body, validation_details
from app.faults import FaultInjector
from app.logs import configure_logging, correlation_id
from app.services.clients import ClientCreation, ClientService
from app.services.gl_reconciliation import GLReconciliationService
from app.services.gl_sync import GLSyncService
from app.services.loans import LoanService
from app.services.operations import OperationStore
from app.services.pipeline import OperationHandler, OperationPipeline
from app.services.reconciliation import ErpPostingRepair, ReconciliationService, RepairService
from app.services.recovery import run_recovery

log = logging.getLogger(__name__)

API_PREFIX = "/api"


async def _recovery_loop(
    gl_sync: GLSyncService,
    pipeline: OperationPipeline,
    handlers: list[OperationHandler],
    interval_seconds: int,
) -> None:
    while True:
        try:
            processed = await asyncio.to_thread(run_recovery, gl_sync, pipeline, handlers)
            if any(processed.values()):
                log.info("Recovery sweep processed %s", processed)
        except Exception:
            log.exception("Recovery sweep failed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    Base.metadata.create_all(engine)

    fineract = FineractClient(
        base_url=settings.fineract_base_url,
        tenant=settings.fineract_tenant,
        username=settings.fineract_username,
        password=settings.fineract_password,
        verify_tls=settings.fineract_tls_verify,
        timeout_seconds=settings.fineract_timeout_seconds,
    )
    erp = ErpClient(settings.erp_base_url, settings.erp_timeout_seconds)
    store = OperationStore(SessionLocal, settings.operation_lease_seconds)
    faults = FaultInjector(settings.fault_injection_enabled)
    pipeline = OperationPipeline(store, erp, settings, faults)
    app.state.gl_sync = GLSyncService(SessionLocal, erp, fineract, pipeline, settings)
    clients = ClientService(pipeline, ClientCreation(SessionLocal, fineract, settings), fineract)
    loans = LoanService(SessionLocal, pipeline, fineract, settings)
    repair = ErpPostingRepair(SessionLocal, fineract, erp)
    app.state.reconciliation = ReconciliationService(SessionLocal, fineract, erp)
    app.state.gl_reconciliation = GLReconciliationService(SessionLocal, fineract, erp, settings.tenant_currency)
    app.state.repairs = RepairService(pipeline, repair)
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.faults = faults
    app.state.clients = clients
    app.state.loans = loans
    app.state.handlers = [*app.state.gl_sync.handlers, *clients.handlers, *loans.handlers, repair]

    recovery = asyncio.create_task(_recovery_loop(app.state.gl_sync, pipeline, app.state.handlers, settings.recovery_interval_seconds))
    yield
    recovery.cancel()
    with suppress(asyncio.CancelledError):
        await recovery


app = FastAPI(title="Integration Service", lifespan=lifespan)
partner = [Depends(require_partner_key)]
app.include_router(gl_accounts_router, prefix=API_PREFIX, dependencies=partner)
app.include_router(clients_router, prefix=API_PREFIX, dependencies=partner)
app.include_router(loans_router, prefix=API_PREFIX, dependencies=partner)
app.include_router(reconciliation_router, prefix=API_PREFIX, dependencies=partner)
app.include_router(admin_router, dependencies=[Depends(require_admin_key)])


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(error_body(exc.status_code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    body = error_body(422, "the request is not valid", code=INVALID_REQUEST, details=validation_details(exc.errors()))
    return JSONResponse(body, status_code=422)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    token = correlation_id.set(request.headers.get("X-Correlation-ID") or str(uuid.uuid4()))
    try:
        try:
            response = await call_next(request)
        except Exception:
            log.exception("Unhandled error")
            response = JSONResponse(error_body(500, "internal error"), status_code=500)
        response.headers["X-Correlation-ID"] = correlation_id.get() or ""
        return response
    finally:
        correlation_id.reset(token)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "integration-service"}
