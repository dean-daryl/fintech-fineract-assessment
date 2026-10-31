from datetime import date
from typing import Any

import pytest

from app.domain.accounting import AccountingMappingError, JournalLine, journal_entry_for, reversal_of
from app.domain.loans import FineractLoanTransaction, parse_loan_transaction
from app.domain.reconciliation import (
    ErpEntry,
    LoanTransaction,
    ReconciliationStatus,
    differences,
    expected_entry,
    is_loan_entry,
    parse_erp_entry,
    reconcile,
)

LOAN = "LN-2026-000001"


def repayment(**overrides: Any) -> FineractLoanTransaction:
    body: dict[str, Any] = {
        "id": 32,
        "externalId": "REPAY-1",
        "type": {"id": 2, "code": "loanTransactionType.repayment", "value": "Repayment"},
        "date": [2026, 5, 1],
        "amount": 458403.0,
        "principalPortion": 383403.0,
        "interestPortion": 75000.0,
        "outstandingLoanBalance": 4616597.0,
        "manuallyReversed": False,
    }
    body.update(overrides)
    return parse_loan_transaction(body)


def disbursement() -> FineractLoanTransaction:
    return repayment(
        id=31,
        externalId="DISB-1",
        type={"id": 1, "code": "loanTransactionType.disbursement", "value": "Disbursement"},
        date=[2026, 4, 1],
        amount=5000000.0,
        principalPortion=0.0,
        interestPortion=0.0,
    )


def reversed_repayment() -> FineractLoanTransaction:
    return repayment(manuallyReversed=True, reversedOnDate=[2026, 5, 3])


def posted(transaction: FineractLoanTransaction, erp_id: str = "JE-000001") -> ErpEntry:
    entry = journal_entry_for(transaction, LOAN)
    return ErpEntry(erp_id, entry.external_reference, entry.entry_date, entry.currency, entry.lines)


def erp_entry(reference: str, lines: tuple[JournalLine, ...], erp_id: str = "JE-000009") -> ErpEntry:
    return ErpEntry(erp_id, reference, date(2026, 5, 1), "RWF", lines)


def statuses(items: list[Any]) -> dict[str | None, ReconciliationStatus]:
    return {item.external_reference: item.status for item in items}


def test_same_lines_in_both_is_matched() -> None:
    items = reconcile(
        [LoanTransaction(LOAN, repayment()), LoanTransaction(LOAN, disbursement())], [posted(repayment()), posted(disbursement())]
    )

    assert statuses(items) == {"REPAY-1": ReconciliationStatus.MATCHED, "DISB-1": ReconciliationStatus.MATCHED}
    assert all(item.differences == () for item in items)


def test_fineract_only_is_missing_in_erp() -> None:
    [item] = reconcile([LoanTransaction(LOAN, repayment())], [])

    assert item.status is ReconciliationStatus.MISSING_IN_ERP
    assert item.loan_reference == LOAN
    assert item.expected == journal_entry_for(repayment(), LOAN)
    assert item.erp is None


def test_erp_only_is_missing_in_fineract() -> None:
    orphan = erp_entry("MANUAL-1", (JournalLine("1100", debit=100), JournalLine("1200", credit=100)))

    [item] = reconcile([], [orphan])

    assert item.status is ReconciliationStatus.MISSING_IN_FINERACT
    assert item.fineract is None
    assert item.erp == orphan


def test_different_total_is_amount_mismatch_with_details() -> None:
    wrong = erp_entry("REPAY-1", (JournalLine("1100", debit=450000), JournalLine("1200", credit=375000), JournalLine("4100", credit=75000)))

    [item] = reconcile([LoanTransaction(LOAN, repayment())], [wrong])

    assert item.status is ReconciliationStatus.AMOUNT_MISMATCH
    assert item.differences == (
        "amount: Fineract 458403, ERP 450000",
        "1100 debit: Fineract 458403, ERP 450000",
        "1200 credit: Fineract 383403, ERP 375000",
    )


def test_same_total_with_a_different_split_is_amount_mismatch() -> None:
    resplit = erp_entry("REPAY-1", (JournalLine("1100", debit=458403), JournalLine("1200", credit=458403)))

    [item] = reconcile([LoanTransaction(LOAN, repayment())], [resplit])

    assert item.status is ReconciliationStatus.AMOUNT_MISMATCH
    assert item.differences == ("1200 credit: Fineract 383403, ERP 458403", "4100 credit: Fineract 75000, ERP 0")


def test_a_different_type_shows_as_different_accounts() -> None:
    as_disbursement = erp_entry("REPAY-1", (JournalLine("1200", debit=458403), JournalLine("1100", credit=458403)))

    [item] = reconcile([LoanTransaction(LOAN, repayment())], [as_disbursement])

    assert item.status is ReconciliationStatus.AMOUNT_MISMATCH
    assert "1100 credit: Fineract 0, ERP 458403" in item.differences


def test_dates_and_descriptions_do_not_affect_the_status() -> None:
    entry = posted(repayment())
    later = ErpEntry(entry.erp_transaction_id, entry.external_reference, date(2026, 6, 1), entry.currency, entry.lines)

    [item] = reconcile([LoanTransaction(LOAN, repayment())], [later])

    assert item.status is ReconciliationStatus.MATCHED


def test_reversed_with_original_and_reversal_posted_is_two_matches() -> None:
    original = journal_entry_for(repayment(), LOAN)
    reversal = reversal_of(original, date(2026, 5, 3))
    erp = [posted(repayment()), ErpEntry("JE-000002", reversal.external_reference, reversal.entry_date, "RWF", reversal.lines)]

    items = reconcile([LoanTransaction(LOAN, reversed_repayment())], erp)

    assert statuses(items) == {"REPAY-1": ReconciliationStatus.MATCHED, "REPAY-1-REV": ReconciliationStatus.MATCHED}


def test_reversed_in_fineract_without_reversal_in_erp_is_missing_in_erp() -> None:
    items = reconcile([LoanTransaction(LOAN, reversed_repayment())], [posted(repayment())])

    assert statuses(items) == {"REPAY-1": ReconciliationStatus.MATCHED, "REPAY-1-REV": ReconciliationStatus.MISSING_IN_ERP}
    rev = next(item for item in items if item.external_reference == "REPAY-1-REV")
    assert rev.expected is not None and rev.expected.entry_date == date(2026, 5, 3)
    assert rev.expected.lines == (
        JournalLine("1100", credit=458403),
        JournalLine("1200", debit=383403),
        JournalLine("4100", debit=75000),
    )


def test_reversed_before_reaching_the_erp_nets_to_zero() -> None:
    [item] = reconcile([LoanTransaction(LOAN, reversed_repayment())], [])

    assert item.status is ReconciliationStatus.MATCHED
    assert item.note is not None and "net to zero" in item.note


def test_erp_reversal_of_a_live_fineract_transaction_is_missing_in_fineract() -> None:
    reversal = reversal_of(journal_entry_for(repayment(), LOAN), date(2026, 5, 3))
    erp = [posted(repayment()), ErpEntry("JE-000002", reversal.external_reference, reversal.entry_date, "RWF", reversal.lines)]

    items = reconcile([LoanTransaction(LOAN, repayment())], erp)

    assert statuses(items) == {"REPAY-1": ReconciliationStatus.MATCHED, "REPAY-1-REV": ReconciliationStatus.MISSING_IN_FINERACT}
    rev = next(item for item in items if item.external_reference == "REPAY-1-REV")
    assert rev.note is not None and "not reversed" in rev.note


def test_fineract_transaction_without_reference_is_missing_in_erp() -> None:
    [item] = reconcile([LoanTransaction(LOAN, repayment(externalId=None))], [])

    assert item.status is ReconciliationStatus.MISSING_IN_ERP
    assert item.external_reference is None
    assert item.note is not None


def test_unmappable_transaction_against_an_erp_entry_is_a_mismatch() -> None:
    with_fee = repayment(feeChargesPortion=1000.0, principalPortion=382403.0)

    [item] = reconcile([LoanTransaction(LOAN, with_fee)], [posted(repayment())])

    assert item.status is ReconciliationStatus.AMOUNT_MISMATCH
    assert item.note is not None and "fee" in item.note


def test_differences_is_empty_for_identical_entries() -> None:
    assert differences(journal_entry_for(repayment(), LOAN), posted(repayment())) == ()


def test_loan_entries_touch_a_loan_account() -> None:
    assert is_loan_entry(posted(repayment()))
    assert not is_loan_entry(erp_entry("RENT-1", (JournalLine("5100", debit=10), JournalLine("1100", credit=10))))


def test_expected_entry_for_a_reversal_reference() -> None:
    entry = expected_entry(LoanTransaction(LOAN, reversed_repayment()), "REPAY-1-REV")

    assert entry.external_reference == "REPAY-1-REV"
    assert entry.entry_date == date(2026, 5, 3)


def test_expected_entry_refuses_a_reversal_of_a_live_transaction() -> None:
    with pytest.raises(AccountingMappingError):
        expected_entry(LoanTransaction(LOAN, repayment()), "REPAY-1-REV")


def test_parse_erp_entry_reads_the_client_request_shape() -> None:
    request = {
        "external_reference": "REPAY-1",
        "entry_date": "2026-05-01",
        "description": "Loan LN-2026-000001 repayment",
        "currency": "RWF",
        "lines": [{"account_code": "1100", "debit": 10, "credit": 0}, {"account_code": "1200", "debit": 0, "credit": 10}],
    }

    entry = parse_erp_entry("JE-000001", request)

    assert entry.amount == 10
    assert entry.lines == (JournalLine("1100", debit=10), JournalLine("1200", credit=10))
