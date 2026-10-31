from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.fineract import FineractClient, FineractTimeoutError
from app.config import Settings
from app.domain.accounting import JournalEntry, journal_entry_for
from app.domain.clients import validate_client
from app.domain.loans import (
    FineractLoanRecord,
    FineractLoanTransaction,
    LoanProductTerms,
    format_loan_reference,
    repayment_problem,
    to_fineract_approval,
    to_fineract_disbursement,
    to_fineract_loan_application,
    to_fineract_repayment,
)
from app.domain.states import OperationStatus, OperationType
from app.models import ClientMap, LoanDisbursement, LoanMap, Operation, loan_reference_sequence
from app.services.pipeline import FineractOutcome, OperationHandler, OperationPipeline, OperationRejected, OperationResult


class LoanNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class LoanView:
    loan_reference: str
    client_uid: str
    record: FineractLoanRecord


class _LoanOperation:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, settings: Settings) -> None:
        self._sessions = session_factory
        self._fineract = fineract
        self._settings = settings

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        return None

    def journal_entry(self, operation: Operation, outcome: FineractOutcome) -> JournalEntry | None:
        if outcome.transaction is None:
            return None
        return journal_entry_for(outcome.transaction, operation.payload["loan_reference"])

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"loan_reference": operation.payload["loan_reference"]}

    def _fineract_loan_id(self, loan_reference: str) -> int:
        with self._sessions() as session:
            fineract_loan_id = session.scalar(select(LoanMap.fineract_loan_id).where(LoanMap.loan_reference == loan_reference))
        if fineract_loan_id is None:
            raise OperationRejected(404, f"loan {loan_reference} not found")
        return fineract_loan_id

    def _date(self, operation: Operation, field: str) -> date:
        requested: str | None = operation.payload.get(field)
        if requested:
            return date.fromisoformat(requested)
        return _today(self._settings)

    def _transaction(self, operation: Operation, expected_type: str) -> FineractOutcome | None:
        transaction = self._fineract.find_loan_transaction(operation.payload["loan_reference"], operation.external_reference)
        if transaction is None or transaction.reversed:
            return None
        if transaction.type.lower() != expected_type:
            raise OperationRejected(409, f"external_reference is already used by a Fineract {transaction.type.lower()} transaction")
        return FineractOutcome(transaction.fineract_id, _transaction_details(transaction), transaction)

    def _visible(self, outcome: FineractOutcome | None) -> FineractOutcome:
        if outcome is None:
            raise FineractTimeoutError("Fineract accepted the command but the transaction is not visible yet")
        return outcome


class LoanCreation(_LoanOperation):
    operation_type = OperationType.CREATE_LOAN

    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, settings: Settings) -> None:
        super().__init__(session_factory, fineract, settings)
        self._terms: LoanProductTerms | None = None

    def prepare(self, operation: Operation) -> None:
        client_uid: str = operation.payload["client_uid"]
        with self._sessions() as session:
            fineract_client_id = session.scalar(select(ClientMap.fineract_client_id).where(ClientMap.client_uid == client_uid))
        if fineract_client_id is None:
            raise OperationRejected(422, f"client {client_uid} was not onboarded through this service")
        validation = validate_client(client_uid, self._fineract.find_client_by_external_id(client_uid), operation.payload["client_name"])
        if not validation.valid:
            raise OperationRejected(
                422,
                f"client {client_uid} failed validation: exists={validation.exists}, "
                f"active={validation.active}, name_matches={validation.name_matches}",
            )
        self._assign_reference(operation)

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        loan_reference = self._reference(operation)
        if loan_reference is None:
            return None
        loan = self._fineract.find_loan_by_external_id(loan_reference)
        return FineractOutcome(loan.fineract_id) if loan is not None else None

    def execute(self, operation: Operation) -> FineractOutcome:
        loan_reference = self._reference(operation)
        assert loan_reference is not None
        with self._sessions() as session:
            fineract_client_id = session.scalar(
                select(ClientMap.fineract_client_id).where(ClientMap.client_uid == operation.payload["client_uid"])
            )
        assert fineract_client_id is not None
        payload = to_fineract_loan_application(
            loan_reference,
            fineract_client_id,
            operation.payload["principal"],
            self._date(operation, "submitted_on_date"),
            self._product_terms(),
        )
        return FineractOutcome(self._fineract.create_loan(payload))

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        with self._sessions() as session, session.begin():
            session.execute(update(LoanMap).where(LoanMap.operation_id == operation.id).values(fineract_loan_id=outcome.resource_id))

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {
            "loan_reference": self._reference(operation),
            "client_uid": operation.payload["client_uid"],
            "principal": operation.payload["principal"],
        }

    def _assign_reference(self, operation: Operation) -> None:
        with self._sessions() as session, session.begin():
            if session.scalar(select(LoanMap.loan_reference).where(LoanMap.operation_id == operation.id)) is not None:
                return
            sequence_value = session.execute(loan_reference_sequence.next_value()).scalar_one()
            session.add(
                LoanMap(
                    loan_reference=format_loan_reference(_today(self._settings).year, sequence_value),
                    client_uid=operation.payload["client_uid"],
                    operation_id=operation.id,
                )
            )

    def _reference(self, operation: Operation) -> str | None:
        with self._sessions() as session:
            return session.scalar(select(LoanMap.loan_reference).where(LoanMap.operation_id == operation.id))

    def _product_terms(self) -> LoanProductTerms:
        if self._terms is None:
            terms = self._fineract.find_loan_product(self._settings.loan_product_short_name)
            if terms is None:
                raise RuntimeError(f"Fineract loan product {self._settings.loan_product_short_name!r} not found; run make bootstrap")
            self._terms = terms
        return self._terms


class LoanApproval(_LoanOperation):
    operation_type = OperationType.APPROVE_LOAN

    def prepare(self, operation: Operation) -> None:
        self._fineract_loan_id(operation.payload["loan_reference"])

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        loan = self._fineract.find_loan_by_external_id(operation.payload["loan_reference"])
        return FineractOutcome(loan.fineract_id) if loan is not None and loan.approved else None

    def execute(self, operation: Operation) -> FineractOutcome:
        fineract_loan_id = self._fineract_loan_id(operation.payload["loan_reference"])
        approved_on = self._date(operation, "approved_on_date")
        self._fineract.loan_command(fineract_loan_id, "approve", to_fineract_approval(approved_on))
        return FineractOutcome(fineract_loan_id, {"approved_on_date": approved_on.isoformat()})


class LoanDisbursal(_LoanOperation):
    operation_type = OperationType.DISBURSE_LOAN

    def prepare(self, operation: Operation) -> None:
        loan_reference: str = operation.payload["loan_reference"]
        with self._sessions() as session, session.begin():
            session.get_one(LoanMap, loan_reference, with_for_update=True)
            session.execute(
                insert(LoanDisbursement)
                .values(loan_reference=loan_reference, operation_id=operation.id)
                .on_conflict_do_nothing(index_elements=["loan_reference"])
            )
            slot = session.get_one(LoanDisbursement, loan_reference)
            if slot.operation_id == operation.id:
                return
            owner_status = session.scalar(select(Operation.status).where(Operation.id == slot.operation_id))
            if owner_status == OperationStatus.FAILED:
                slot.operation_id = operation.id
                return
        raise OperationRejected(409, f"loan {loan_reference} is already disbursed or being disbursed by another request")

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        return self._transaction(operation, "disbursement")

    def execute(self, operation: Operation) -> FineractOutcome:
        fineract_loan_id = self._fineract_loan_id(operation.payload["loan_reference"])
        payload = to_fineract_disbursement(operation.external_reference, self._date(operation, "disbursed_on_date"))
        self._fineract.loan_command(fineract_loan_id, "disburse", payload)
        return self._visible(self.look_up(operation))


class LoanRepayment(_LoanOperation):
    operation_type = OperationType.REPAY_LOAN

    def prepare(self, operation: Operation) -> None:
        loan_reference: str = operation.payload["loan_reference"]
        self._fineract_loan_id(loan_reference)
        loan = self._fineract.find_loan_by_external_id(loan_reference)
        if loan is None:
            raise OperationRejected(404, f"loan {loan_reference} not found in Fineract")
        problem = repayment_problem(loan, operation.payload["amount"])
        if problem is not None:
            raise OperationRejected(422, problem)

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        return self._transaction(operation, "repayment")

    def execute(self, operation: Operation) -> FineractOutcome:
        fineract_loan_id = self._fineract_loan_id(operation.payload["loan_reference"])
        payload = to_fineract_repayment(
            operation.external_reference,
            operation.payload["amount"],
            self._date(operation, "transaction_date"),
        )
        self._fineract.repay_loan(fineract_loan_id, payload)
        return self._visible(self.look_up(operation))

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"loan_reference": operation.payload["loan_reference"], "amount": operation.payload["amount"]}


class LoanService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        pipeline: OperationPipeline,
        fineract: FineractClient,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._pipeline = pipeline
        self._fineract = fineract
        self._creation = LoanCreation(session_factory, fineract, settings)
        self._approval = LoanApproval(session_factory, fineract, settings)
        self._disbursal = LoanDisbursal(session_factory, fineract, settings)
        self._repayment = LoanRepayment(session_factory, fineract, settings)

    @property
    def handlers(self) -> list[OperationHandler]:
        return [self._creation, self._approval, self._disbursal, self._repayment]

    def create(self, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        return self._pipeline.submit(self._creation, external_reference, payload)

    def approve(self, loan_reference: str, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        self._require(loan_reference)
        return self._pipeline.submit(self._approval, external_reference, {"loan_reference": loan_reference, **payload})

    def disburse(self, loan_reference: str, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        self._require(loan_reference)
        return self._pipeline.submit(self._disbursal, external_reference, {"loan_reference": loan_reference, **payload})

    def repay(self, loan_reference: str, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        self._require(loan_reference)
        return self._pipeline.submit(self._repayment, external_reference, {"loan_reference": loan_reference, **payload})

    def get(self, loan_reference: str) -> LoanView:
        client_uid = self._require(loan_reference)
        record = self._fineract.find_loan_by_external_id(loan_reference)
        if record is None:
            raise LoanNotFoundError(loan_reference)
        return LoanView(loan_reference=loan_reference, client_uid=client_uid, record=record)

    def _require(self, loan_reference: str) -> str:
        with self._sessions() as session:
            row = session.get(LoanMap, loan_reference)
        if row is None or row.fineract_loan_id is None:
            raise LoanNotFoundError(loan_reference)
        return row.client_uid


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.tenant_timezone)).date()


def _transaction_details(transaction: FineractLoanTransaction) -> dict[str, Any]:
    return {
        "transaction_date": transaction.date.isoformat() if transaction.date else None,
        "amount": transaction.amount,
        "principal_portion": transaction.principal_portion,
        "interest_portion": transaction.interest_portion,
        "outstanding_after": transaction.outstanding_after,
    }
