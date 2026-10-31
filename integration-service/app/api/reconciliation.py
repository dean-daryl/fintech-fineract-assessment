from datetime import date
from typing import Annotated, Self

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.clients.erp import ErpError
from app.clients.fineract import FineractError
from app.domain.accounting import JournalLine
from app.domain.loans import LOAN_REFERENCE_PATTERN
from app.domain.reconciliation import ReconciliationItem, ReconciliationStatus, base_reference
from app.services.reconciliation import ReconciliationReport, ReconciliationService, RepairService

router = APIRouter(tags=["reconciliation"])

Reference = Annotated[str, Field(min_length=1, max_length=100)]
MAX_REFERENCES = 500


class ReconciliationIn(BaseModel):
    from_date: date | None = None
    to_date: date | None = None
    external_references: list[Reference] | None = Field(default=None, min_length=1, max_length=MAX_REFERENCES)

    @model_validator(mode="after")
    def _one_filter(self) -> Self:
        has_range = self.from_date is not None or self.to_date is not None
        if has_range == (self.external_references is not None):
            raise ValueError("send either from_date and to_date, or external_references")
        if has_range and (self.from_date is None or self.to_date is None):
            raise ValueError("from_date and to_date go together")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date is after to_date")
        return self


class LineOut(BaseModel):
    account_code: str
    debit: int
    credit: int


class FineractSideOut(BaseModel):
    type: str
    transaction_date: date | None
    amount: int
    principal_portion: int
    interest_portion: int
    overpayment_portion: int
    reversed: bool
    reversed_on_date: date | None
    expected_journal_lines: list[LineOut] | None


class ErpSideOut(BaseModel):
    erp_transaction_id: str
    entry_date: date
    amount: int
    lines: list[LineOut]


class ReconciliationItemOut(BaseModel):
    external_reference: str | None
    loan_reference: str | None
    status: ReconciliationStatus
    operation_status: str | None
    fineract: FineractSideOut | None
    erp: ErpSideOut | None
    differences: list[str]
    note: str | None


class ReconciliationCriteriaOut(BaseModel):
    from_date: date | None
    to_date: date | None
    external_references: list[str] | None


class ReconciliationOut(BaseModel):
    criteria: ReconciliationCriteriaOut
    loans_scanned: int
    summary: dict[ReconciliationStatus, int]
    items: list[ReconciliationItemOut]
    not_found: list[str]


class RepairIn(BaseModel):
    external_reference: Reference
    loan_reference: str = Field(pattern=LOAN_REFERENCE_PATTERN)
    transaction_reference: Reference


def _lines(lines: tuple[JournalLine, ...]) -> list[LineOut]:
    return [LineOut(account_code=line.account_code, debit=line.debit, credit=line.credit) for line in lines]


def _item(item: ReconciliationItem, operation_statuses: dict[str, str]) -> ReconciliationItemOut:
    t = item.fineract
    reference = item.external_reference
    return ReconciliationItemOut(
        external_reference=reference,
        loan_reference=item.loan_reference,
        status=item.status,
        operation_status=operation_statuses.get(base_reference(reference)) if reference else None,
        fineract=FineractSideOut(
            type=t.type,
            transaction_date=t.date,
            amount=t.amount,
            principal_portion=t.principal_portion,
            interest_portion=t.interest_portion,
            overpayment_portion=t.overpayment_portion,
            reversed=t.reversed,
            reversed_on_date=t.reversed_on,
            expected_journal_lines=_lines(item.expected.lines) if item.expected else None,
        )
        if t is not None
        else None,
        erp=ErpSideOut(
            erp_transaction_id=item.erp.erp_transaction_id,
            entry_date=item.erp.entry_date,
            amount=item.erp.amount,
            lines=_lines(item.erp.lines),
        )
        if item.erp is not None
        else None,
        differences=list(item.differences),
        note=item.note,
    )


def _out(body: ReconciliationIn, report: ReconciliationReport) -> ReconciliationOut:
    summary = {s: 0 for s in ReconciliationStatus}
    for item in report.items:
        summary[item.status] += 1
    return ReconciliationOut(
        criteria=ReconciliationCriteriaOut(from_date=body.from_date, to_date=body.to_date, external_references=body.external_references),
        loans_scanned=report.loans_scanned,
        summary=summary,
        items=[_item(item, report.operation_statuses) for item in report.items],
        not_found=report.not_found,
    )


@router.post("/reconciliation", response_model=ReconciliationOut)
def reconcile(body: ReconciliationIn, request: Request) -> ReconciliationOut:
    service: ReconciliationService = request.app.state.reconciliation
    try:
        if body.external_references is not None:
            report = service.by_references(list(dict.fromkeys(body.external_references)))
        else:
            assert body.from_date is not None and body.to_date is not None
            report = service.by_date_range(body.from_date, body.to_date)
    except FineractError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Fineract is unavailable; no partial report is returned") from exc
    except ErpError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the ERP is unavailable; no partial report is returned") from exc
    return _out(body, report)


@router.post("/reconciliation/repairs", status_code=status.HTTP_201_CREATED)
def repair(body: RepairIn, request: Request) -> JSONResponse:
    service: RepairService = request.app.state.repairs
    result = service.repair(body.external_reference, body.model_dump(exclude={"external_reference"}))
    return JSONResponse(result.body, status_code=result.status_code)
