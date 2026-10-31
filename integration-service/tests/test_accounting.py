from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from app.domain.accounting import (
    AccountingMappingError,
    JournalEntry,
    JournalLine,
    journal_entry_for,
    reversal_of,
    to_erp_journal_entry,
)
from app.domain.loans import FineractLoanTransaction, parse_loan_transaction

LOAN_REFERENCE = "LN-2026-000001"


def fineract_transaction(**overrides: Any) -> FineractLoanTransaction:
    body: dict[str, Any] = {
        "id": 32,
        "externalId": "REPAY-1",
        "type": {"id": 2, "code": "loanTransactionType.repayment", "value": "Repayment", "repayment": True},
        "date": [2026, 5, 1],
        "amount": 458403.0,
        "principalPortion": 383403.0,
        "interestPortion": 75000.0,
        "feeChargesPortion": 0.0,
        "penaltyChargesPortion": 0.0,
        "overpaymentPortion": 0.0,
        "outstandingLoanBalance": 4616597.0,
        "manuallyReversed": False,
    }
    body.update(overrides)
    return parse_loan_transaction(body)


def disbursement(**overrides: Any) -> FineractLoanTransaction:
    fields: dict[str, Any] = {
        "id": 31,
        "externalId": "DISB-1",
        "type": {"id": 1, "code": "loanTransactionType.disbursement", "value": "Disbursement", "disbursement": True},
        "date": [2026, 4, 1],
        "amount": 5000000.0,
        "principalPortion": 0.0,
        "interestPortion": 0.0,
        "outstandingLoanBalance": 5000000.0,
    }
    fields.update(overrides)
    return fineract_transaction(**fields)


def assert_balanced(entry: JournalEntry) -> None:
    assert sum(line.debit for line in entry.lines) == sum(line.credit for line in entry.lines)


def test_disbursement_debits_loan_receivable_and_credits_bank() -> None:
    entry = journal_entry_for(disbursement(), LOAN_REFERENCE)

    assert entry == JournalEntry(
        external_reference="DISB-1",
        entry_date=date(2026, 4, 1),
        description="Loan LN-2026-000001 disbursement",
        currency="RWF",
        lines=(JournalLine("1200", debit=5000000), JournalLine("1100", credit=5000000)),
    )


def test_disbursement_accepts_principal_portion_equal_to_amount() -> None:
    entry = journal_entry_for(disbursement(principalPortion=5000000.0), LOAN_REFERENCE)

    assert entry.lines == (JournalLine("1200", debit=5000000), JournalLine("1100", credit=5000000))


def test_repayment_splits_credit_by_fineract_portions() -> None:
    entry = journal_entry_for(fineract_transaction(), LOAN_REFERENCE)

    assert entry.external_reference == "REPAY-1"
    assert entry.entry_date == date(2026, 5, 1)
    assert entry.description == "Loan LN-2026-000001 repayment"
    assert entry.lines == (
        JournalLine("1100", debit=458403),
        JournalLine("1200", credit=383403),
        JournalLine("4100", credit=75000),
    )
    assert_balanced(entry)


def test_repayment_without_interest_has_no_interest_line() -> None:
    entry = journal_entry_for(fineract_transaction(amount=100000.0, principalPortion=100000.0, interestPortion=0.0), LOAN_REFERENCE)

    assert entry.lines == (JournalLine("1100", debit=100000), JournalLine("1200", credit=100000))


def test_repayment_of_interest_only_has_no_principal_line() -> None:
    entry = journal_entry_for(fineract_transaction(amount=75000.0, principalPortion=0.0, interestPortion=75000.0), LOAN_REFERENCE)

    assert entry.lines == (JournalLine("1100", debit=75000), JournalLine("4100", credit=75000))


def test_overpayment_portion_goes_to_overpayment_liability() -> None:
    entry = journal_entry_for(
        fineract_transaction(amount=500000.0, principalPortion=400000.0, interestPortion=60000.0, overpaymentPortion=40000.0),
        LOAN_REFERENCE,
    )

    assert entry.lines == (
        JournalLine("1100", debit=500000),
        JournalLine("1200", credit=400000),
        JournalLine("4100", credit=60000),
        JournalLine("2100", credit=40000),
    )
    assert_balanced(entry)


def test_repayment_portions_must_add_up_to_amount() -> None:
    with pytest.raises(AccountingMappingError, match="add up to 458402"):
        journal_entry_for(fineract_transaction(principalPortion=383402.0), LOAN_REFERENCE)


@pytest.mark.parametrize("portion", ["feeChargesPortion", "penaltyChargesPortion"])
def test_repayment_with_charges_is_not_mapped(portion: str) -> None:
    with pytest.raises(AccountingMappingError, match="fee or penalty"):
        journal_entry_for(fineract_transaction(amount=468403.0, **{portion: 10000.0}), LOAN_REFERENCE)


def test_negative_portion_is_rejected() -> None:
    with pytest.raises(AccountingMappingError, match="positive"):
        journal_entry_for(fineract_transaction(amount=100000.0, principalPortion=-10000.0, interestPortion=110000.0), LOAN_REFERENCE)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"interestPortion": 1000.0}, "interest"),
        ({"overpaymentPortion": 1000.0}, "overpayment"),
        ({"principalPortion": 4000000.0}, "differs from its amount"),
    ],
)
def test_disbursement_with_unexpected_portions_is_not_mapped(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(AccountingMappingError, match=message):
        journal_entry_for(disbursement(**overrides), LOAN_REFERENCE)


def test_reversed_transaction_is_not_mapped() -> None:
    with pytest.raises(AccountingMappingError, match="reversal_of"):
        journal_entry_for(fineract_transaction(manuallyReversed=True), LOAN_REFERENCE)


def test_unsupported_transaction_type_is_not_mapped() -> None:
    accrual = fineract_transaction(type={"id": 10, "code": "loanTransactionType.accrual", "value": "Accrual"})

    with pytest.raises(AccountingMappingError, match="'Accrual' has no accounting mapping"):
        journal_entry_for(accrual, LOAN_REFERENCE)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"external_id": None}, "no external reference"),
        ({"date": None}, "no date"),
        ({"amount": 0}, "not positive"),
    ],
)
def test_incomplete_transaction_is_not_mapped(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(AccountingMappingError, match=message):
        journal_entry_for(replace(fineract_transaction(), **changes), LOAN_REFERENCE)


def test_reversal_swaps_every_line_under_its_own_reference() -> None:
    original = journal_entry_for(fineract_transaction(), LOAN_REFERENCE)

    reversal = reversal_of(original, date(2026, 5, 3))

    assert reversal.external_reference == "REPAY-1-REV"
    assert reversal.entry_date == date(2026, 5, 3)
    assert reversal.description == "Reversal of REPAY-1: Loan LN-2026-000001 repayment"
    assert reversal.lines == (
        JournalLine("1100", credit=458403),
        JournalLine("1200", debit=383403),
        JournalLine("4100", debit=75000),
    )
    assert_balanced(reversal)


def test_erp_payload_matches_the_erp_journal_entry_request() -> None:
    payload = to_erp_journal_entry(journal_entry_for(disbursement(), LOAN_REFERENCE))

    assert payload == {
        "external_reference": "DISB-1",
        "entry_date": "2026-04-01",
        "description": "Loan LN-2026-000001 disbursement",
        "currency": "RWF",
        "lines": [
            {"account_code": "1200", "debit": 5000000, "credit": 0},
            {"account_code": "1100", "debit": 0, "credit": 5000000},
        ],
    }
