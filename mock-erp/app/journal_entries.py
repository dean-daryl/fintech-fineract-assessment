from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.journal import Line, entry_problem, payload_hash
from app.models import Account, JournalEntry, JournalLine

router = APIRouter(prefix="/api/journal-entries", tags=["journal-entries"])

SessionDep = Annotated[Session, Depends(get_session)]

ENTRY_ID_PREFIX = "JE-"


class JournalLineIn(BaseModel):
    account_code: str = Field(min_length=1, max_length=45)
    debit: int = Field(default=0, ge=0)
    credit: int = Field(default=0, ge=0)


class JournalEntryIn(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    entry_date: date
    description: str = Field(min_length=1, max_length=500)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: list[JournalLineIn]


class JournalLineOut(BaseModel):
    account_code: str
    account_name: str
    debit: int
    credit: int


class JournalEntryOut(BaseModel):
    id: str
    external_reference: str
    entry_date: date
    description: str
    currency: str
    lines: list[JournalLineOut]
    total_debit: int
    total_credit: int
    created_at: datetime


def _out(entry: JournalEntry) -> JournalEntryOut:
    return JournalEntryOut(
        id=f"{ENTRY_ID_PREFIX}{entry.id:06d}",
        external_reference=entry.external_reference,
        entry_date=entry.entry_date,
        description=entry.description,
        currency=entry.currency,
        lines=[
            JournalLineOut(account_code=line.account.code, account_name=line.account.name, debit=line.debit, credit=line.credit)
            for line in entry.lines
        ],
        total_debit=sum(line.debit for line in entry.lines),
        total_credit=sum(line.credit for line in entry.lines),
        created_at=entry.created_at,
    )


def _find(session: Session, external_reference: str) -> JournalEntry | None:
    return session.scalar(select(JournalEntry).where(JournalEntry.external_reference == external_reference))


@router.post("", response_model=JournalEntryOut, status_code=status.HTTP_201_CREATED)
def post_journal_entry(payload: JournalEntryIn, session: SessionDep, response: Response) -> JournalEntryOut:
    codes = {line.account_code for line in payload.lines}
    accounts = {account.code: account for account in session.scalars(select(Account).where(Account.code.in_(codes)))}
    lines = [Line(line.account_code, line.debit, line.credit) for line in payload.lines]
    problem = entry_problem(payload.currency, lines, {code: account.currency for code, account in accounts.items()})
    if problem is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)

    request_hash = payload_hash(payload.model_dump(mode="json"))
    entry = JournalEntry(
        external_reference=payload.external_reference,
        entry_date=payload.entry_date,
        description=payload.description,
        currency=payload.currency,
        payload_hash=request_hash,
        lines=[
            JournalLine(line_number=number, account=accounts[line.account_code], debit=line.debit, credit=line.credit)
            for number, line in enumerate(payload.lines, start=1)
        ],
    )
    session.add(entry)
    try:
        session.commit()
        return _out(entry)
    except IntegrityError:
        session.rollback()
        existing = _find(session, payload.external_reference)
        if existing is None:
            raise

    if existing.payload_hash != request_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"journal entry {payload.external_reference} already exists with different details",
        )
    response.status_code = status.HTTP_200_OK
    return _out(existing)


@router.get("", response_model=list[JournalEntryOut])
def list_journal_entries(
    session: SessionDep,
    from_date: Annotated[date, Query()],
    to_date: Annotated[date, Query()],
) -> list[JournalEntryOut]:
    if from_date > to_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "from_date is after to_date")
    entries = session.scalars(
        select(JournalEntry)
        .where(JournalEntry.entry_date >= from_date, JournalEntry.entry_date <= to_date)
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    return [_out(entry) for entry in entries]


@router.get("/{external_reference}", response_model=JournalEntryOut)
def get_journal_entry(external_reference: str, session: SessionDep) -> JournalEntryOut:
    entry = _find(session, external_reference)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"journal entry {external_reference} not found")
    return _out(entry)
