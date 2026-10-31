"""Fineract loan transactions against ERP journal entries, keyed by external reference. Pure functions, no I/O."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any

from app.domain.accounting import (
    INTEREST_INCOME,
    LOAN_OVERPAYMENTS,
    LOAN_RECEIVABLE,
    REVERSAL_SUFFIX,
    AccountingMappingError,
    JournalEntry,
    JournalLine,
    journal_entry_for,
    reversal_of,
)
from app.domain.loans import FineractLoanTransaction

RECONCILED_TRANSACTION_TYPES = frozenset({"disbursement", "repayment"})
LOAN_ACCOUNTS = frozenset({LOAN_RECEIVABLE, INTEREST_INCOME, LOAN_OVERPAYMENTS})


class ReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"


@dataclass(frozen=True)
class LoanTransaction:
    loan_reference: str
    transaction: FineractLoanTransaction


@dataclass(frozen=True)
class ErpEntry:
    erp_transaction_id: str
    external_reference: str
    entry_date: date
    currency: str
    lines: tuple[JournalLine, ...]

    @property
    def amount(self) -> int:
        return sum(line.debit for line in self.lines)


@dataclass(frozen=True)
class ReconciliationItem:
    external_reference: str | None
    loan_reference: str | None
    status: ReconciliationStatus
    fineract: FineractLoanTransaction | None
    expected: JournalEntry | None
    erp: ErpEntry | None
    differences: tuple[str, ...] = ()
    note: str | None = None


def is_reconciled_transaction(transaction: FineractLoanTransaction) -> bool:
    return transaction.type.lower() in RECONCILED_TRANSACTION_TYPES


def is_loan_entry(entry: ErpEntry) -> bool:
    return any(line.account_code in LOAN_ACCOUNTS for line in entry.lines)


def base_reference(external_reference: str) -> str:
    return external_reference.removesuffix(REVERSAL_SUFFIX)


def is_reversal_reference(external_reference: str) -> bool:
    return external_reference.endswith(REVERSAL_SUFFIX)


def parse_erp_entry(erp_transaction_id: str, request: Mapping[str, Any]) -> ErpEntry:
    return ErpEntry(
        erp_transaction_id=erp_transaction_id,
        external_reference=str(request["external_reference"]),
        entry_date=date.fromisoformat(str(request["entry_date"])),
        currency=str(request["currency"]),
        lines=tuple(
            JournalLine(str(line["account_code"]), debit=int(line["debit"]), credit=int(line["credit"])) for line in request["lines"]
        ),
    )


def expected_entries(item: LoanTransaction) -> list[JournalEntry]:
    transaction = item.transaction
    original = journal_entry_for(replace(transaction, reversed=False), item.loan_reference)
    if not transaction.reversed:
        return [original]
    if transaction.reversed_on is None:
        raise AccountingMappingError("the reversed Fineract transaction has no reversal date")
    return [original, reversal_of(original, transaction.reversed_on)]


def expected_entry(item: LoanTransaction, external_reference: str) -> JournalEntry:
    entry = next((e for e in expected_entries(item) if e.external_reference == external_reference), None)
    if entry is None:
        raise AccountingMappingError(f"Fineract expects no journal entry {external_reference}: the transaction is not reversed")
    return entry


def differences(expected: JournalEntry, actual: ErpEntry) -> tuple[str, ...]:
    found: list[str] = []
    expected_total = sum(line.debit for line in expected.lines)
    if expected_total != actual.amount:
        found.append(f"amount: Fineract {expected_total}, ERP {actual.amount}")
    if expected.currency != actual.currency:
        found.append(f"currency: Fineract {expected.currency}, ERP {actual.currency}")
    expected_sides = _sides(expected.lines)
    actual_sides = _sides(actual.lines)
    for account, side in sorted(expected_sides.keys() | actual_sides.keys()):
        want = expected_sides.get((account, side), 0)
        got = actual_sides.get((account, side), 0)
        if want != got:
            found.append(f"{account} {side}: Fineract {want}, ERP {got}")
    return tuple(found)


def reconcile(transactions: Iterable[LoanTransaction], erp_entries: Iterable[ErpEntry]) -> list[ReconciliationItem]:
    erp_by_reference = {entry.external_reference: entry for entry in erp_entries}
    items: list[ReconciliationItem] = []
    expected_references: set[str] = set()

    for item in transactions:
        transaction = item.transaction
        if not transaction.external_id:
            items.append(
                ReconciliationItem(
                    None,
                    item.loan_reference,
                    ReconciliationStatus.MISSING_IN_ERP,
                    transaction,
                    None,
                    None,
                    note="the Fineract transaction has no external reference, so no ERP entry can carry it",
                )
            )
            continue
        expected_references.add(transaction.external_id)
        try:
            entries = expected_entries(item)
        except AccountingMappingError as exc:
            items.append(_unmapped(item, erp_by_reference.get(transaction.external_id), str(exc)))
            continue
        expected_references.update(entry.external_reference for entry in entries)

        if transaction.reversed and not any(entry.external_reference in erp_by_reference for entry in entries):
            items.append(
                ReconciliationItem(
                    transaction.external_id,
                    item.loan_reference,
                    ReconciliationStatus.MATCHED,
                    transaction,
                    None,
                    None,
                    note="reversed in Fineract before it reached the ERP; both sides net to zero",
                )
            )
            continue
        for entry in entries:
            items.append(_compare(item, entry, erp_by_reference.get(entry.external_reference)))

    for reference, erp in erp_by_reference.items():
        if reference in expected_references:
            continue
        note = None
        if is_reversal_reference(reference) and base_reference(reference) in expected_references:
            note = "the ERP holds a reversal, but the Fineract transaction is not reversed"
        items.append(ReconciliationItem(reference, None, ReconciliationStatus.MISSING_IN_FINERACT, None, None, erp, note=note))
    return items


def _compare(item: LoanTransaction, expected: JournalEntry, erp: ErpEntry | None) -> ReconciliationItem:
    if erp is None:
        return ReconciliationItem(
            expected.external_reference, item.loan_reference, ReconciliationStatus.MISSING_IN_ERP, item.transaction, expected, None
        )
    found = differences(expected, erp)
    status = ReconciliationStatus.AMOUNT_MISMATCH if found else ReconciliationStatus.MATCHED
    return ReconciliationItem(expected.external_reference, item.loan_reference, status, item.transaction, expected, erp, found)


def _unmapped(item: LoanTransaction, erp: ErpEntry | None, error: str) -> ReconciliationItem:
    status = ReconciliationStatus.AMOUNT_MISMATCH if erp is not None else ReconciliationStatus.MISSING_IN_ERP
    return ReconciliationItem(
        item.transaction.external_id,
        item.loan_reference,
        status,
        item.transaction,
        None,
        erp,
        note=f"no journal entry can be built from the Fineract transaction: {error}",
    )


def _sides(lines: Iterable[JournalLine]) -> dict[tuple[str, str], int]:
    sides: dict[tuple[str, str], int] = {}
    for line in lines:
        if line.debit:
            sides[(line.account_code, "debit")] = sides.get((line.account_code, "debit"), 0) + line.debit
        if line.credit:
            sides[(line.account_code, "credit")] = sides.get((line.account_code, "credit"), 0) + line.credit
    return sides
