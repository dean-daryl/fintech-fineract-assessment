"""Fineract loan transactions to ERP journal entries. Pure functions, no I/O."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.loans import FineractLoanTransaction

CURRENCY = "RWF"
BANK = "1100"
LOAN_RECEIVABLE = "1200"
LOAN_OVERPAYMENTS = "2100"
INTEREST_INCOME = "4100"
REVERSAL_SUFFIX = "-REV"


class AccountingMappingError(ValueError):
    pass


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: int = 0
    credit: int = 0


@dataclass(frozen=True)
class JournalEntry:
    external_reference: str
    entry_date: date
    description: str
    currency: str
    lines: tuple[JournalLine, ...]


def journal_entry_for(transaction: FineractLoanTransaction, loan_reference: str) -> JournalEntry:
    if transaction.reversed:
        raise AccountingMappingError("a reversed Fineract transaction has no journal entry of its own; post reversal_of instead")
    if not transaction.external_id:
        raise AccountingMappingError("the Fineract transaction has no external reference to key the journal entry on")
    if transaction.date is None:
        raise AccountingMappingError("the Fineract transaction has no date")
    if transaction.amount <= 0:
        raise AccountingMappingError(f"the Fineract transaction amount {transaction.amount} is not positive")

    kind = transaction.type.lower()
    if kind == "disbursement":
        lines = _disbursement_lines(transaction)
    elif kind == "repayment":
        lines = _repayment_lines(transaction)
    else:
        raise AccountingMappingError(f"Fineract transaction type {transaction.type!r} has no accounting mapping")

    return _balanced(
        JournalEntry(
            external_reference=transaction.external_id,
            entry_date=transaction.date,
            description=f"Loan {loan_reference} {kind}",
            currency=CURRENCY,
            lines=lines,
        )
    )


def reversal_of(entry: JournalEntry, reversed_on: date) -> JournalEntry:
    return _balanced(
        JournalEntry(
            external_reference=f"{entry.external_reference}{REVERSAL_SUFFIX}",
            entry_date=reversed_on,
            description=f"Reversal of {entry.external_reference}: {entry.description}",
            currency=entry.currency,
            lines=tuple(JournalLine(line.account_code, debit=line.credit, credit=line.debit) for line in entry.lines),
        )
    )


def to_erp_journal_entry(entry: JournalEntry) -> dict[str, Any]:
    return {
        "external_reference": entry.external_reference,
        "entry_date": entry.entry_date.isoformat(),
        "description": entry.description,
        "currency": entry.currency,
        "lines": [{"account_code": line.account_code, "debit": line.debit, "credit": line.credit} for line in entry.lines],
    }


def _disbursement_lines(transaction: FineractLoanTransaction) -> tuple[JournalLine, ...]:
    other = {
        "interest": transaction.interest_portion,
        "fee": transaction.fee_charges_portion,
        "penalty": transaction.penalty_charges_portion,
        "overpayment": transaction.overpayment_portion,
    }
    unexpected = sorted(name for name, amount in other.items() if amount)
    if unexpected:
        raise AccountingMappingError(f"a disbursement with {', '.join(unexpected)} portions has no accounting mapping")
    if transaction.principal_portion not in (0, transaction.amount):
        raise AccountingMappingError(
            f"disbursement principal portion {transaction.principal_portion} differs from its amount {transaction.amount}"
        )
    return (
        JournalLine(LOAN_RECEIVABLE, debit=transaction.amount),
        JournalLine(BANK, credit=transaction.amount),
    )


def _repayment_lines(transaction: FineractLoanTransaction) -> tuple[JournalLine, ...]:
    if transaction.fee_charges_portion or transaction.penalty_charges_portion:
        raise AccountingMappingError("a repayment with fee or penalty portions has no accounting mapping")
    allocated = transaction.principal_portion + transaction.interest_portion + transaction.overpayment_portion
    if allocated != transaction.amount:
        raise AccountingMappingError(f"repayment portions add up to {allocated}, not to the transaction amount {transaction.amount}")
    credits = (
        JournalLine(LOAN_RECEIVABLE, credit=transaction.principal_portion),
        JournalLine(INTEREST_INCOME, credit=transaction.interest_portion),
        JournalLine(LOAN_OVERPAYMENTS, credit=transaction.overpayment_portion),
    )
    return (JournalLine(BANK, debit=transaction.amount), *(line for line in credits if line.credit))


def _balanced(entry: JournalEntry) -> JournalEntry:
    for line in entry.lines:
        if line.debit < 0 or line.credit < 0 or (line.debit > 0) == (line.credit > 0):
            raise AccountingMappingError(f"line on {line.account_code} must have either a positive debit or a positive credit")
    debits = sum(line.debit for line in entry.lines)
    credits = sum(line.credit for line in entry.lines)
    if debits != credits:
        raise AccountingMappingError(f"entry is unbalanced: debits {debits} != credits {credits}")
    return entry
