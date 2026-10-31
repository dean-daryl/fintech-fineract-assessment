import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.erp import ErpClient, ErpError
from app.clients.fineract import FineractClient
from app.domain.accounting import REVERSAL_SUFFIX, JournalEntry, to_erp_journal_entry
from app.domain.reconciliation import (
    ErpEntry,
    LoanTransaction,
    ReconciliationItem,
    base_reference,
    expected_entry,
    is_loan_entry,
    is_reconciled_transaction,
    is_reversal_reference,
    parse_erp_entry,
    reconcile,
)
from app.domain.states import OperationType
from app.models import LoanMap, Operation
from app.services.pipeline import FineractOutcome, OperationPipeline, OperationRejected, OperationResult

log = logging.getLogger(__name__)

POSTING_OPERATION_TYPES = (OperationType.DISBURSE_LOAN, OperationType.REPAY_LOAN)


@dataclass(frozen=True)
class ReconciliationReport:
    items: list[ReconciliationItem]
    operation_statuses: dict[str, str]
    not_found: list[str]
    loans_scanned: int


class FineractLoanIndex:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient) -> None:
        self._sessions = session_factory
        self._fineract = fineract

    def all_transactions(self) -> tuple[list[LoanTransaction], int]:
        with self._sessions() as session:
            loan_references = list(
                session.scalars(
                    select(LoanMap.loan_reference).where(LoanMap.fineract_loan_id.is_not(None)).order_by(LoanMap.loan_reference)
                )
            )
        found: list[LoanTransaction] = []
        for loan_reference in loan_references:
            loan = self._fineract.find_loan_by_external_id(loan_reference)
            if loan is None:
                continue
            found.extend(LoanTransaction(loan_reference, t) for t in loan.transactions if is_reconciled_transaction(t))
        return found, len(loan_references)

    def transaction(self, loan_reference: str, external_reference: str) -> LoanTransaction | None:
        transaction = self._fineract.find_loan_transaction(loan_reference, external_reference)
        if transaction is None or not is_reconciled_transaction(transaction):
            return None
        return LoanTransaction(loan_reference, transaction)


class ReconciliationService:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, erp: ErpClient) -> None:
        self._sessions = session_factory
        self._loans = FineractLoanIndex(session_factory, fineract)
        self._erp = erp

    def by_date_range(self, from_date: date, to_date: date) -> ReconciliationReport:
        transactions, loans_scanned = self._loans.all_transactions()
        by_reference = {t.transaction.external_id: t for t in transactions if t.transaction.external_id}
        selected = {_key(t): t for t in transactions if t.transaction.date is not None and from_date <= t.transaction.date <= to_date}
        erp = {e.external_reference: e for e in self._erp_entries(from_date, to_date) if is_loan_entry(e)}
        for reference in erp:
            other_dated = by_reference.get(base_reference(reference))
            if other_dated is not None:
                selected.setdefault(_key(other_dated), other_dated)
        self._fill_from_erp(erp, selected.values())
        return self._report(list(selected.values()), erp, [], loans_scanned)

    def by_references(self, references: Collection[str]) -> ReconciliationReport:
        transactions, loans_scanned = self._loans.all_transactions()
        by_reference = {t.transaction.external_id: t for t in transactions if t.transaction.external_id}
        selected: dict[str, LoanTransaction] = {}
        erp: dict[str, ErpEntry] = {}
        for reference in references:
            match = by_reference.get(base_reference(reference))
            if match is not None:
                selected.setdefault(_key(match), match)
            entry = self._erp_entry(reference)
            if entry is not None:
                erp[reference] = entry
        self._fill_from_erp(erp, selected.values())
        not_found = [r for r in references if r not in erp and base_reference(r) not in by_reference]
        return self._report(list(selected.values()), erp, not_found, loans_scanned)

    def _fill_from_erp(self, erp: dict[str, ErpEntry], transactions: Collection[LoanTransaction]) -> None:
        for item in transactions:
            reference = item.transaction.external_id
            if reference is None:
                continue
            for candidate in (reference, f"{reference}{REVERSAL_SUFFIX}") if item.transaction.reversed else (reference,):
                if candidate not in erp:
                    entry = self._erp_entry(candidate)
                    if entry is not None:
                        erp[candidate] = entry

    def _report(
        self, transactions: list[LoanTransaction], erp: dict[str, ErpEntry], not_found: list[str], loans_scanned: int
    ) -> ReconciliationReport:
        items = reconcile(transactions, erp.values())
        references = {base_reference(i.external_reference) for i in items if i.external_reference}
        return ReconciliationReport(items, self._operation_statuses(references), not_found, loans_scanned)

    def _erp_entries(self, from_date: date, to_date: date) -> list[ErpEntry]:
        return [parse_erp_entry(e.erp_transaction_id, e.request) for e in self._erp.list_journal_entries(from_date, to_date)]

    def _erp_entry(self, reference: str) -> ErpEntry | None:
        found = self._erp.find_journal_entry(reference)
        return parse_erp_entry(found.erp_transaction_id, found.request) if found is not None else None

    def _operation_statuses(self, references: Collection[str]) -> dict[str, str]:
        if not references:
            return {}
        with self._sessions() as session:
            rows = session.execute(
                select(Operation.external_reference, Operation.status).where(
                    Operation.operation_type.in_(POSTING_OPERATION_TYPES),
                    Operation.external_reference.in_(references),
                )
            ).all()
        return {reference: status for reference, status in rows}


class ErpPostingRepair:
    operation_type = OperationType.REPAIR_ERP_POSTING

    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, erp: ErpClient) -> None:
        self._sessions = session_factory
        self._loans = FineractLoanIndex(session_factory, fineract)
        self._erp = erp

    def prepare(self, operation: Operation) -> None:
        loan_reference: str = operation.payload["loan_reference"]
        reference: str = operation.payload["transaction_reference"]
        with self._sessions() as session:
            if session.get(LoanMap, loan_reference) is None:
                raise OperationRejected(404, f"loan {loan_reference} not found")
        expected = self._expected(operation)
        try:
            existing = self._erp.find_journal_entry(reference)
        except ErpError as exc:
            raise OperationRejected(503, f"the ERP could not be checked before the repair: {exc}") from exc
        if existing is not None and existing.request != to_erp_journal_entry(expected):
            raise OperationRejected(
                409, f"the ERP already holds a different journal entry {reference}; repair only posts entries missing from the ERP"
            )

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        item = self._loans.transaction(operation.payload["loan_reference"], base_reference(operation.payload["transaction_reference"]))
        if item is None:
            return None
        transaction = item.transaction
        return FineractOutcome(
            transaction.fineract_id,
            {
                "transaction_type": transaction.type,
                "transaction_date": transaction.date.isoformat() if transaction.date else None,
                "amount": transaction.amount,
                "principal_portion": transaction.principal_portion,
                "interest_portion": transaction.interest_portion,
                "reversed": transaction.reversed,
            },
            transaction,
        )

    def execute(self, operation: Operation) -> FineractOutcome:
        outcome = self.look_up(operation)
        if outcome is None:
            raise OperationRejected(404, self._not_in_fineract(operation))
        return outcome

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        return None

    def journal_entry(self, operation: Operation, outcome: FineractOutcome) -> JournalEntry | None:
        if outcome.transaction is None:
            return None
        return expected_entry(
            LoanTransaction(operation.payload["loan_reference"], outcome.transaction), operation.payload["transaction_reference"]
        )

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {
            "loan_reference": operation.payload["loan_reference"],
            "transaction_reference": operation.payload["transaction_reference"],
        }

    def _expected(self, operation: Operation) -> JournalEntry:
        outcome = self.look_up(operation)
        if outcome is None or outcome.transaction is None:
            raise OperationRejected(404, self._not_in_fineract(operation))
        try:
            entry = self.journal_entry(operation, outcome)
        except ValueError as exc:
            raise OperationRejected(409, f"nothing to repair: {exc}") from exc
        assert entry is not None
        return entry

    @staticmethod
    def _not_in_fineract(operation: Operation) -> str:
        reference = operation.payload["transaction_reference"]
        kind = "reversed transaction" if is_reversal_reference(reference) else "disbursement or repayment"
        return f"loan {operation.payload['loan_reference']} has no {kind} {base_reference(reference)} in Fineract"


class RepairService:
    def __init__(self, pipeline: OperationPipeline, handler: ErpPostingRepair) -> None:
        self._pipeline = pipeline
        self._handler = handler

    def repair(self, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        return self._pipeline.submit(self._handler, external_reference, payload)


def _key(item: LoanTransaction) -> str:
    return f"{item.loan_reference}:{item.transaction.external_id or item.transaction.fineract_id}"
