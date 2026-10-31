import pytest

from app.journal import Line, balance, entry_problem, payload_hash

RWF = {"1100": "RWF", "1200": "RWF", "4100": "RWF"}
DISBURSEMENT = [Line("1200", 5_000_000, 0), Line("1100", 0, 5_000_000)]


def test_balanced_entry_is_accepted() -> None:
    assert entry_problem("RWF", DISBURSEMENT, RWF) is None


def test_repayment_split_across_principal_and_interest_is_accepted() -> None:
    lines = [Line("1100", 458_403, 0), Line("1200", 0, 383_403), Line("4100", 0, 75_000)]
    assert entry_problem("RWF", lines, RWF) is None


def test_unbalanced_entry_is_rejected() -> None:
    lines = [Line("1200", 5_000_000, 0), Line("1100", 0, 4_999_999)]
    assert entry_problem("RWF", lines, RWF) == "entry is unbalanced: debits 5000000 != credits 4999999"


@pytest.mark.parametrize(
    ("lines", "problem"),
    [
        ([Line("1200", 100, 0)], "a journal entry needs at least two lines"),
        ([Line("1200", 100, 100), Line("1100", 0, 0)], "line 1 must have either a debit or a credit"),
        ([Line("1200", 100, 0), Line("1100", 0, 0)], "line 2 must have either a debit or a credit"),
        ([Line("1200", -100, 0), Line("1100", 0, -100)], "line 1 has a negative amount"),
        ([Line("1200", 100, 0), Line("9999", 0, 100)], "unknown account codes: 9999"),
    ],
)
def test_malformed_entries_are_rejected(lines: list[Line], problem: str) -> None:
    assert entry_problem("RWF", lines, RWF) == problem


def test_account_in_another_currency_is_rejected() -> None:
    assert entry_problem("RWF", DISBURSEMENT, {**RWF, "1100": "USD"}) == "accounts not in RWF: 1100"


def test_hash_ignores_key_order_but_not_values() -> None:
    assert payload_hash({"a": 1, "b": [2, 3]}) == payload_hash({"b": [2, 3], "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


@pytest.mark.parametrize(
    ("account_type", "expected"),
    [("ASSET", 70), ("EXPENSE", 70), ("LIABILITY", -70), ("EQUITY", -70), ("INCOME", -70)],
)
def test_balance_is_signed_by_the_normal_side(account_type: str, expected: int) -> None:
    assert balance(account_type, debit_total=100, credit_total=30) == expected
