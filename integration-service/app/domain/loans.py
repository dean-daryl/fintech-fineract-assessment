"""Partner loan requests to Fineract loan commands, and Fineract loan data parsing. Pure functions, no I/O."""

import datetime
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.clients import FINERACT_DATE_FORMAT, FINERACT_LOCALE, parse_fineract_date

LOAN_REFERENCE_PATTERN = r"^LN-\d{4}-\d{6}$"
FINERACT_LOAN_TYPE_INDIVIDUAL = "individual"


class AmountError(ValueError):
    pass


@dataclass(frozen=True)
class LoanProductTerms:
    product_id: int
    number_of_repayments: int
    repayment_every: int
    repayment_frequency_type: int
    interest_rate_per_period: Decimal
    interest_type: int
    amortization_type: int
    interest_calculation_period_type: int
    transaction_processing_strategy_code: str


@dataclass(frozen=True)
class FineractLoanTransaction:
    fineract_id: int
    external_id: str | None
    type: str
    date: date | None
    amount: int
    principal_portion: int
    interest_portion: int
    fee_charges_portion: int
    penalty_charges_portion: int
    overpayment_portion: int
    outstanding_after: int
    reversed: bool
    reversed_on: datetime.date | None = None


@dataclass(frozen=True)
class FineractLoanSummary:
    principal_disbursed: int
    principal_paid: int
    principal_outstanding: int
    interest_charged: int
    interest_paid: int
    interest_outstanding: int
    total_repaid: int
    total_outstanding: int


@dataclass(frozen=True)
class FineractLoanRecord:
    fineract_id: int
    external_id: str | None
    client_external_id: str | None
    principal: int
    status: str
    approved: bool
    active: bool
    closed: bool
    summary: FineractLoanSummary
    transactions: list[FineractLoanTransaction]


def format_loan_reference(year: int, sequence_value: int) -> str:
    return f"LN-{year:04d}-{sequence_value:06d}"


def to_amount(value: Any) -> int:
    if value is None:
        return 0
    amount = Decimal(str(value))
    if amount != amount.to_integral_value():
        raise AmountError(f"Fineract amount {value} has a fractional part; RWF has no minor unit")
    return int(amount)


def _id_of(value: Any) -> int:
    return int(value["id"]) if isinstance(value, dict) else int(value)


def parse_loan_product(body: dict[str, Any]) -> LoanProductTerms:
    return LoanProductTerms(
        product_id=int(body["id"]),
        number_of_repayments=int(body["numberOfRepayments"]),
        repayment_every=int(body["repaymentEvery"]),
        repayment_frequency_type=_id_of(body["repaymentFrequencyType"]),
        interest_rate_per_period=Decimal(str(body["interestRatePerPeriod"])),
        interest_type=_id_of(body["interestType"]),
        amortization_type=_id_of(body["amortizationType"]),
        interest_calculation_period_type=_id_of(body["interestCalculationPeriodType"]),
        transaction_processing_strategy_code=str(body["transactionProcessingStrategyCode"]),
    )


def to_fineract_loan_application(
    loan_reference: str,
    fineract_client_id: int,
    principal: int,
    submitted_on: date,
    terms: LoanProductTerms,
) -> dict[str, Any]:
    return {
        "externalId": loan_reference,
        "clientId": fineract_client_id,
        "productId": terms.product_id,
        "loanType": FINERACT_LOAN_TYPE_INDIVIDUAL,
        "principal": principal,
        "loanTermFrequency": terms.number_of_repayments * terms.repayment_every,
        "loanTermFrequencyType": terms.repayment_frequency_type,
        "numberOfRepayments": terms.number_of_repayments,
        "repaymentEvery": terms.repayment_every,
        "repaymentFrequencyType": terms.repayment_frequency_type,
        "interestRatePerPeriod": str(terms.interest_rate_per_period),
        "interestType": terms.interest_type,
        "amortizationType": terms.amortization_type,
        "interestCalculationPeriodType": terms.interest_calculation_period_type,
        "transactionProcessingStrategyCode": terms.transaction_processing_strategy_code,
        "submittedOnDate": submitted_on.isoformat(),
        "expectedDisbursementDate": submitted_on.isoformat(),
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def to_fineract_approval(approved_on: date) -> dict[str, Any]:
    return {"approvedOnDate": approved_on.isoformat(), "dateFormat": FINERACT_DATE_FORMAT, "locale": FINERACT_LOCALE}


def to_fineract_disbursement(external_reference: str, disbursed_on: date) -> dict[str, Any]:
    return {
        "actualDisbursementDate": disbursed_on.isoformat(),
        "externalId": external_reference,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def to_fineract_repayment(external_reference: str, amount: int, transaction_date: date) -> dict[str, Any]:
    return {
        "transactionDate": transaction_date.isoformat(),
        "transactionAmount": amount,
        "externalId": external_reference,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": FINERACT_LOCALE,
    }


def parse_loan_transaction(body: dict[str, Any]) -> FineractLoanTransaction:
    transaction_type = body.get("type") or {}
    return FineractLoanTransaction(
        fineract_id=int(body["id"]),
        external_id=body.get("externalId"),
        type=str(transaction_type.get("value") or transaction_type.get("code") or "unknown"),
        date=parse_fineract_date(body.get("date")),
        amount=to_amount(body.get("amount")),
        principal_portion=to_amount(body.get("principalPortion")),
        interest_portion=to_amount(body.get("interestPortion")),
        fee_charges_portion=to_amount(body.get("feeChargesPortion")),
        penalty_charges_portion=to_amount(body.get("penaltyChargesPortion")),
        overpayment_portion=to_amount(body.get("overpaymentPortion")),
        outstanding_after=to_amount(body.get("outstandingLoanBalance")),
        reversed=bool(body.get("manuallyReversed")),
        reversed_on=parse_fineract_date(body.get("reversedOnDate")),
    )


def parse_fineract_loan(body: dict[str, Any]) -> FineractLoanRecord:
    status = body.get("status") or {}
    summary = body.get("summary") or {}
    return FineractLoanRecord(
        fineract_id=int(body["id"]),
        external_id=body.get("externalId"),
        client_external_id=body.get("clientExternalId"),
        principal=to_amount(body.get("principal")),
        status=str(status.get("value") or status.get("code") or "unknown"),
        approved=bool(status.get("waitingForDisbursal") or status.get("active") or status.get("closed") or status.get("overpaid")),
        active=bool(status.get("active")),
        closed=bool(status.get("closed")),
        summary=FineractLoanSummary(
            principal_disbursed=to_amount(summary.get("principalDisbursed")),
            principal_paid=to_amount(summary.get("principalPaid")),
            principal_outstanding=to_amount(summary.get("principalOutstanding")),
            interest_charged=to_amount(summary.get("interestCharged")),
            interest_paid=to_amount(summary.get("interestPaid")),
            interest_outstanding=to_amount(summary.get("interestOutstanding")),
            total_repaid=to_amount(summary.get("totalRepayment")),
            total_outstanding=to_amount(summary.get("totalOutstanding")),
        ),
        transactions=[parse_loan_transaction(t) for t in body.get("transactions") or []],
    )


def repayment_problem(loan: FineractLoanRecord, amount: int) -> str | None:
    if not loan.active:
        return f"loan is {loan.status}, not active"
    if amount > loan.summary.total_outstanding:
        return f"amount {amount} exceeds the outstanding balance {loan.summary.total_outstanding}"
    return None
