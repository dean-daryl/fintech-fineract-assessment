"""Journal entry rules. Pure functions, no I/O."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEBIT_NORMAL_TYPES = frozenset({"ASSET", "EXPENSE"})


@dataclass(frozen=True)
class Line:
    account_code: str
    debit: int
    credit: int


def entry_problem(currency: str, lines: Sequence[Line], account_currencies: Mapping[str, str]) -> str | None:
    if len(lines) < 2:
        return "a journal entry needs at least two lines"
    for number, line in enumerate(lines, start=1):
        if line.debit < 0 or line.credit < 0:
            return f"line {number} has a negative amount"
        if (line.debit > 0) == (line.credit > 0):
            return f"line {number} must have either a debit or a credit"
    unknown = sorted({line.account_code for line in lines} - set(account_currencies))
    if unknown:
        return f"unknown account codes: {', '.join(unknown)}"
    other_currency = sorted({line.account_code for line in lines if account_currencies[line.account_code] != currency})
    if other_currency:
        return f"accounts not in {currency}: {', '.join(other_currency)}"
    debits = sum(line.debit for line in lines)
    credits = sum(line.credit for line in lines)
    if debits != credits:
        return f"entry is unbalanced: debits {debits} != credits {credits}"
    return None


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def balance(account_type: str, debit_total: int, credit_total: int) -> int:
    if account_type in DEBIT_NORMAL_TYPES:
        return debit_total - credit_total
    return credit_total - debit_total
