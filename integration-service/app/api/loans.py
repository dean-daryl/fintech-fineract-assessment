from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.clients.fineract import FineractError
from app.domain.clients import CLIENT_UID_PATTERN, FINERACT_FULLNAME_MAX_LENGTH, normalise_name
from app.domain.loans import LOAN_REFERENCE_PATTERN
from app.services.loans import LoanNotFoundError, LoanService
from app.services.pipeline import OperationResult

router = APIRouter(tags=["loans"])

LoanReference = Annotated[str, Path(pattern=LOAN_REFERENCE_PATTERN)]
ExternalReference = Annotated[str, Field(min_length=1, max_length=100)]


class CreateLoanIn(BaseModel):
    external_reference: ExternalReference
    client_uid: str = Field(pattern=CLIENT_UID_PATTERN)
    client_name: str = Field(min_length=1, max_length=FINERACT_FULLNAME_MAX_LENGTH)
    principal: int = Field(gt=0)
    submitted_on_date: date | None = None

    @field_validator("client_name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        return normalise_name(value)


class ApproveLoanIn(BaseModel):
    external_reference: ExternalReference
    approved_on_date: date | None = None


class DisburseLoanIn(BaseModel):
    external_reference: ExternalReference
    disbursed_on_date: date | None = None


class RepayLoanIn(BaseModel):
    external_reference: ExternalReference
    amount: int = Field(gt=0)
    transaction_date: date | None = None


class LoanBalancesOut(BaseModel):
    principal_disbursed: int
    principal_paid: int
    principal_outstanding: int
    interest_charged: int
    interest_paid: int
    interest_outstanding: int
    total_repaid: int
    total_outstanding: int


class LoanTransactionOut(BaseModel):
    external_reference: str | None
    type: str
    transaction_date: date | None
    amount: int
    principal_portion: int
    interest_portion: int
    outstanding_after: int
    reversed: bool


class LoanOut(BaseModel):
    loan_reference: str
    client_uid: str
    principal: int
    status: str
    balances: LoanBalancesOut
    transactions: list[LoanTransactionOut]


def _service(request: Request) -> LoanService:
    service: LoanService = request.app.state.loans
    return service


def _respond(result: OperationResult) -> JSONResponse:
    return JSONResponse(result.body, status_code=result.status_code)


def _dated(body: BaseModel, *fields: str) -> dict[str, Any]:
    values = body.model_dump(exclude={"external_reference"})
    for field in fields:
        values[field] = values[field].isoformat() if values[field] else None
    return values


@router.post("/loans", status_code=status.HTTP_201_CREATED)
def create_loan(body: CreateLoanIn, request: Request) -> JSONResponse:
    return _respond(_service(request).create(body.external_reference, _dated(body, "submitted_on_date")))


@router.post("/loans/{loan_reference}/approval", status_code=status.HTTP_201_CREATED)
def approve_loan(loan_reference: LoanReference, body: ApproveLoanIn, request: Request) -> JSONResponse:
    try:
        result = _service(request).approve(loan_reference, body.external_reference, _dated(body, "approved_on_date"))
    except LoanNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"loan {loan_reference} not found") from exc
    return _respond(result)


@router.post("/loans/{loan_reference}/disbursement", status_code=status.HTTP_201_CREATED)
def disburse_loan(loan_reference: LoanReference, body: DisburseLoanIn, request: Request) -> JSONResponse:
    try:
        result = _service(request).disburse(loan_reference, body.external_reference, _dated(body, "disbursed_on_date"))
    except LoanNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"loan {loan_reference} not found") from exc
    return _respond(result)


@router.post("/loans/{loan_reference}/repayments", status_code=status.HTTP_201_CREATED)
def repay_loan(loan_reference: LoanReference, body: RepayLoanIn, request: Request) -> JSONResponse:
    try:
        result = _service(request).repay(loan_reference, body.external_reference, _dated(body, "transaction_date"))
    except LoanNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"loan {loan_reference} not found") from exc
    return _respond(result)


@router.get("/loans/{loan_reference}", response_model=LoanOut)
def get_loan(loan_reference: LoanReference, request: Request) -> LoanOut:
    try:
        view = _service(request).get(loan_reference)
    except LoanNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"loan {loan_reference} not found") from exc
    except FineractError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Fineract is unavailable") from exc
    record = view.record
    return LoanOut(
        loan_reference=view.loan_reference,
        client_uid=view.client_uid,
        principal=record.principal,
        status=record.status,
        balances=LoanBalancesOut(**vars(record.summary)),
        transactions=[
            LoanTransactionOut(
                external_reference=t.external_id,
                type=t.type,
                transaction_date=t.date,
                amount=t.amount,
                principal_portion=t.principal_portion,
                interest_portion=t.interest_portion,
                outstanding_after=t.outstanding_after,
                reversed=t.reversed,
            )
            for t in record.transactions
        ],
    )
