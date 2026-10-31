import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response

from app.api.admin import router as admin_router
from app.api.clients import router as clients_router
from app.api.gl_accounts import router as gl_accounts_router
from app.api.loans import router as loans_router
from app.clients.erp import ErpClient
from app.clients.fineract import FineractClient
from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.faults import FaultInjector
from app.logs import configure_logging, correlation_id
from app.services.clients import ClientCreation, ClientService
from app.services.gl_sync import GLSyncService
from app.services.loans import LoanService
from app.services.operations import OperationStore
from app.services.pipeline import OperationHandler, OperationPipeline
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
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.faults = faults
    app.state.clients = clients
    app.state.loans = loans
    app.state.handlers = [*app.state.gl_sync.handlers, *clients.handlers, *loans.handlers]

    recovery = asyncio.create_task(_recovery_loop(app.state.gl_sync, pipeline, app.state.handlers, settings.recovery_interval_seconds))
    yield
    recovery.cancel()
    with suppress(asyncio.CancelledError):
        await recovery


app = FastAPI(title="Integration Service", lifespan=lifespan)
app.include_router(gl_accounts_router, prefix=API_PREFIX)
app.include_router(clients_router, prefix=API_PREFIX)
app.include_router(loans_router, prefix=API_PREFIX)
app.include_router(admin_router)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    token = correlation_id.set(request.headers.get("X-Correlation-ID") or str(uuid.uuid4()))
    try:
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id.get() or ""
        return response
    finally:
        correlation_id.reset(token)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "integration-service"}
