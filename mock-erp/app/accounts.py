from datetime import datetime
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

SessionDep = Annotated[Session, Depends(get_session)]


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class AccountIn(BaseModel):
    code: str = Field(min_length=1, max_length=45)
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    external_reference: str = Field(min_length=1, max_length=100)


class AccountOut(AccountIn):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime


def _same_payload(account: Account, payload: AccountIn) -> bool:
    return (account.code, account.name, account.type, account.currency) == (
        payload.code,
        payload.name,
        payload.type.value,
        payload.currency,
    )


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountIn, session: SessionDep, response: Response) -> Account:
    account = Account(**payload.model_dump(mode="json"))
    session.add(account)
    try:
        session.commit()
        return account
    except IntegrityError:
        session.rollback()

    existing = session.scalar(select(Account).where(Account.external_reference == payload.external_reference))
    if existing is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"account code {payload.code} is used by another account")
    if not _same_payload(existing, payload):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"account {payload.external_reference} already exists with different details",
        )
    response.status_code = status.HTTP_200_OK
    return existing


@router.get("", response_model=list[AccountOut])
def list_accounts(session: SessionDep) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.id)))


@router.get("/{external_reference}", response_model=AccountOut)
def get_account(external_reference: str, session: SessionDep) -> Account:
    account = session.scalar(select(Account).where(Account.external_reference == external_reference))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {external_reference} not found")
    return account
