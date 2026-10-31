"""Exercises the Mock ERP on its own: the seeded chart of accounts, idempotent journal entries, rejection of
unbalanced or malformed entries, and account balances.

Each run uses new references. Its two entries offset each other (1300 <- 1100, then 1100 <- 1300), so account
balances end where they started and later accounting runs are not distorted. Entries keep the SIM-ERP- prefix.
Uses only the standard library so it runs on the host without a virtualenv.
"""

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
RUN = f"{int(time.time()) % 1_000_000:06d}"
AMOUNT = 250_000
TIMES = 10
TODAY = datetime.date.today().isoformat()

checks: list[tuple[str, bool]] = []


def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(f"{ERP_URL}{path}", method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def entry(reference: str, debit_code: str, credit_code: str, amount: int = AMOUNT) -> dict[str, Any]:
    return {
        "external_reference": reference,
        "entry_date": TODAY,
        "description": f"Simulation transfer {credit_code} -> {debit_code}",
        "currency": "RWF",
        "lines": [{"account_code": debit_code, "debit": amount}, {"account_code": credit_code, "credit": amount}],
    }


def balances() -> dict[str, int]:
    result = {}
    for code in ("1100", "1300"):
        _, body = call("GET", f"/api/accounts/{code}/balance")
        result[code] = body["balance"]
    return result


def main() -> int:
    print(f"Run {RUN}: ERP at {ERP_URL}\n")

    print("1. Seeded chart of accounts")
    code, accounts = call("GET", "/api/accounts")
    listed = ", ".join(f"{account['code']} {account['name']}" for account in accounts)
    print(f"  GET /api/accounts -> {code}: {listed}")
    seeded = {f"GL-{c}" for c in ("1100", "1200", "1300", "2100", "4100", "4200", "4300", "4400", "5100")}
    check("all 9 seeded accounts present", seeded <= {a["external_reference"] for a in accounts})
    before = balances()
    print(f"  balances before: {before}")

    print(f"\n2. Same journal entry sent {TIMES} times in parallel")
    first = entry(f"SIM-ERP-{RUN}-1", "1300", "1100")
    print(f"  POST /api/journal-entries {json.dumps(first)}")
    with ThreadPoolExecutor(max_workers=TIMES) as pool:
        results = list(pool.map(lambda _: call("POST", "/api/journal-entries", first), range(TIMES)))
    codes = sorted(code for code, _ in results)
    print(f"  -> status codes {codes}")
    check(f"one 201, the other {TIMES - 1} 200", codes.count(201) == 1 and codes.count(200) == TIMES - 1)
    check("every response has the same body", all(body == results[0][1] for _, body in results))
    after_first = balances()
    print(f"  balances after: {after_first}")
    check(
        f"posted once: 1300 up {AMOUNT}, 1100 down {AMOUNT}",
        after_first == {"1100": before["1100"] - AMOUNT, "1300": before["1300"] + AMOUNT},
    )

    print("\n3. Read it back by external_reference")
    code, body = call("GET", f"/api/journal-entries/{first['external_reference']}")
    print(f"  GET /api/journal-entries/{first['external_reference']} -> {code} {json.dumps(body)}")
    check("found, balanced, two lines", code == 200 and body["total_debit"] == body["total_credit"] == AMOUNT and len(body["lines"]) == 2)

    print("\n4. Same reference, different amount")
    code, body = call("POST", "/api/journal-entries", entry(first["external_reference"], "1300", "1100", AMOUNT + 1))
    print(f"  -> {code} {json.dumps(body)}")
    check("409", code == 409)

    print("\n5. Rejected entries are never stored")
    unbalanced = entry(f"SIM-ERP-{RUN}-UNBALANCED", "1300", "1100")
    unbalanced["lines"][1]["credit"] = AMOUNT - 1
    rejected = {
        "unbalanced": unbalanced,
        "unknown account": entry(f"SIM-ERP-{RUN}-UNKNOWN", "9999", "1100"),
        "single line": {**entry(f"SIM-ERP-{RUN}-SINGLE", "1300", "1100"), "lines": [{"account_code": "1300", "debit": AMOUNT}]},
        "debit and credit on one line": {
            **entry(f"SIM-ERP-{RUN}-BOTH", "1300", "1100"),
            "lines": [{"account_code": "1300", "debit": AMOUNT, "credit": AMOUNT}, {"account_code": "1100", "credit": AMOUNT}],
        },
    }
    for name, payload in rejected.items():
        code, body = call("POST", "/api/journal-entries", payload)
        print(f"  {name}: POST -> {code} {json.dumps(body)}")
        stored, _ = call("GET", f"/api/journal-entries/{payload['external_reference']}")
        check(f"{name}: 422 and not stored (GET -> {stored})", code == 422 and stored == 404)
    check("balances unchanged by rejected entries", balances() == after_first)

    print("\n6. Offsetting entry")
    code, body = call("POST", "/api/journal-entries", entry(f"SIM-ERP-{RUN}-2", "1100", "1300"))
    print(f"  -> {code}")
    after = balances()
    print(f"  balances after: {after}")
    check("201, and balances are back where they started", code == 201 and after == before)

    print("\n7. Unknown account balance")
    code, _ = call("GET", "/api/accounts/9999/balance")
    check("404", code == 404)

    return summary()


def summary() -> int:
    passed = sum(ok for _, ok in checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    for name, ok in checks:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
