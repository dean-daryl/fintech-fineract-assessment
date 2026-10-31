from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.accounts import router as accounts_router
from app.db import Base, SessionLocal, engine
from app.journal_entries import router as journal_entries_router
from app.seed import seed_chart_of_accounts


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(engine)
    seed_chart_of_accounts(SessionLocal)
    yield


app = FastAPI(title="Mock ERP", lifespan=lifespan)
app.include_router(accounts_router)
app.include_router(journal_entries_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-erp"}
