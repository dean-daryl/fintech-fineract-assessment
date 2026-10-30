from datetime import date
from decimal import Decimal

import pytest

from app.domain.loans import (
    AmountError,
    LoanProductTerms,
    format_loan_reference,
    parse_fineract_loan,
    parse_loan_product,
    repayment_problem,
    to_amount,
    to_fineract_loan_application,
)

TERMS = LoanProductTerms(
    product_id=1,
    number_of_repayments=12,
    repayment_every=1,
    repayment_frequency_type=2,
    interest_rate_per_period=Decimal("18"),
    interest_type=0,
    amortization_type=1,
    interest_calculation_period_type=1,
    transaction_processing_strategy_code="mifos-standard-strategy",
)

LOAN_BODY = {
    "id": 9,
    "externalId": "LN-2026-000001",
    "clientExternalId": "SME-000001",
    "principal": 5000000.0,
    "status": {"code": "loanStatusType.active", "value": "Active", "active": True, "closed": False},
    "summary": {
        "principalDisbursed": 5000000.0,
        "principalPaid": 383403.0,
        "principalOutstanding": 4616597.0,
        "interestCharged": 500836.0,
        "interestPaid": 75000.0,
        "interestOutstanding": 425836.0,
        "totalRepayment": 458403.0,
        "totalOutstanding": 5042433.0,
    },
    "transactions": [
        {
            "id": 31,
            "externalId": "DISB-1",
            "type": {"value": "Disbursement"},
            "date": [2026, 4, 1],
            "amount": 5000000.0,
            "principalPortion": 0,
            "interestPortion": 0,
            "outstandingLoanBalance": 5000000.0,
            "manuallyReversed": False,
        }
    ],
}


def test_loan_reference_format() -> None:
    assert format_loan_reference(2026, 1) == "LN-2026-000001"


def test_amount_must_be_whole_francs() -> None:
    assert to_amount(75000.0) == 75000
    assert to_amount(None) == 0
    with pytest.raises(AmountError):
        to_amount(75000.5)


def test_product_terms_come_from_fineract_product() -> None:
    body = {
        "id": 1,
        "numberOfRepayments": 12,
        "repaymentEvery": 1,
        "repaymentFrequencyType": {"id": 2, "value": "Months"},
        "interestRatePerPeriod": 18.0,
        "interestType": {"id": 0},
        "amortizationType": {"id": 1},
        "interestCalculationPeriodType": {"id": 1},
        "transactionProcessingStrategyCode": "mifos-standard-strategy",
    }
    assert parse_loan_product(body) == TERMS


def test_loan_application_uses_product_terms_and_loan_reference() -> None:
    payload = to_fineract_loan_application("LN-2026-000001", 7, 5_000_000, date(2026, 4, 1), TERMS)
    assert payload["externalId"] == "LN-2026-000001"
    assert payload["clientId"] == 7
    assert payload["principal"] == 5_000_000
    assert (payload["loanTermFrequency"], payload["numberOfRepayments"], payload["repaymentFrequencyType"]) == (12, 12, 2)
    assert payload["interestRatePerPeriod"] == "18"
    assert payload["submittedOnDate"] == payload["expectedDisbursementDate"] == "2026-04-01"


def test_parses_loan_with_balances_and_transactions() -> None:
    loan = parse_fineract_loan(LOAN_BODY)
    assert (loan.status, loan.active, loan.approved, loan.closed) == ("Active", True, True, False)
    assert loan.summary.total_outstanding == 5_042_433
    assert loan.transactions[0].external_id == "DISB-1"
    assert loan.transactions[0].date == date(2026, 4, 1)


def test_repayment_problem() -> None:
    loan = parse_fineract_loan(LOAN_BODY)
    assert repayment_problem(loan, 5_042_433) is None
    assert repayment_problem(loan, 5_042_434) is not None
    closed = parse_fineract_loan({**LOAN_BODY, "status": {"value": "Closed (obligations met)", "closed": True}})
    assert repayment_problem(closed, 1) is not None
