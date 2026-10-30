"""Runs one loan end to end through the integration service and records Fineract's own state after every stage.

Client onboarding, application, approval, disbursement, three partial repayments and a final payoff of
Fineract's total outstanding, ending in "Closed (obligations met)". Along the way it checks that a repeated
request executes once, that ten parallel disbursements with different references disburse once, that a
reused reference with a different payload gets 409, and that no response carries a Fineract numeric id.

Every run uses a new client_uid and new references, so it can be re-run on the same stack.
Uses only the standard library so it runs on the host without a virtualenv.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = base64.b64encode(
    f"{os.environ.get('FINERACT_API_USERNAME', 'integration-api')}:{os.environ.get('FINERACT_API_PASSWORD', '')}".encode()
).decode()
SELF_SIGNED = ssl._create_unverified_context()

RUN = f"{int(time.time()) % 1_000_000:06d}"
CLIENT_UID = f"SME-{RUN}"
CLIENT_NAME = f"Simulation Traders {RUN} Ltd"
PRINCIPAL = 5_000_000
PARTIAL_REPAYMENT = 500_000
ACTIVATION_DATE = "2026-03-01"
LOAN_DATE = "2026-04-01"
REPAYMENT_DATES = ["2026-05-01", "2026-06-01", "2026-07-01"]
PAYOFF_DATE = "2026-08-01"
FINERACT_ID_KEYS = {"id", "resourceId", "clientId", "loanId", "officeId", "fineract_id", "fineract_client_id", "fineract_loan_id"}

checks: list[tuple[str, bool]] = []
responses: list[Any] = []


def call(method: str, url: str, body: dict[str, Any] | None = None, fineract: bool = False) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    if fineract:
        request.add_header("Authorization", f"Basic {FINERACT_AUTH}")
        request.add_header("Fineract-Platform-TenantId", "default")
    try:
        with urllib.request.urlopen(request, context=SELF_SIGNED if fineract else None, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def ours(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    code, payload = call(method, f"{INTEGRATION_URL}{path}", body)
    responses.append(payload)
    return code, payload


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def show(label: str, code: int, body: Any) -> None:
    print(f"  {label} -> {code} {json.dumps(body)}")


def fineract_state(loan_reference: str) -> dict[str, Any]:
    code, loan = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}?associations=transactions", fineract=True)
    if code != 200:
        return {"http": code}
    summary = loan.get("summary") or {}
    return {
        "status": loan["status"]["value"],
        "principalDisbursed": summary.get("principalDisbursed"),
        "principalOutstanding": summary.get("principalOutstanding"),
        "interestOutstanding": summary.get("interestOutstanding"),
        "totalRepayment": summary.get("totalRepayment"),
        "totalOutstanding": summary.get("totalOutstanding"),
        "transactions": [
            (t["type"]["value"], t.get("externalId"), t.get("amount"), t.get("principalPortion"), t.get("interestPortion"))
            for t in loan.get("transactions") or []
            if not t.get("manuallyReversed")
        ],
    }


def stage(title: str, loan_reference: str | None) -> dict[str, Any]:
    state = fineract_state(loan_reference) if loan_reference else {}
    print(f"\n  Fineract after {title}:")
    for key, value in state.items():
        print(f"    {key:<22} {value}")
    return state


def find_keys(value: Any, keys: set[str]) -> set[str]:
    if isinstance(value, dict):
        found = {key for key in value if key in keys}
        for child in value.values():
            found |= find_keys(child, keys)
        return found
    if isinstance(value, list):
        return set().union(*(find_keys(child, keys) for child in value)) if value else set()
    return set()


def parallel(times: int, fn: Any) -> list[tuple[int, Any]]:
    with ThreadPoolExecutor(max_workers=times) as pool:
        return list(pool.map(fn, range(times)))


def main() -> int:
    print(f"Run {RUN}: client {CLIENT_UID} {CLIENT_NAME!r}, principal {PRINCIPAL} RWF, product SAF (18%/year, 12 months)\n")

    print("1. Onboard the client")
    client = {"external_reference": f"SIM-{RUN}-CLIENT", "client_uid": CLIENT_UID, "name": CLIENT_NAME, "activation_date": ACTIVATION_DATE}
    code, body = ours("POST", "/clients", client)
    show("POST /clients", code, body)
    check("client created (201)", code == 201)
    code, body = ours("GET", f"/clients/{CLIENT_UID}/validation?name={urllib.parse.quote(CLIENT_NAME)}")
    show("GET /clients/{uid}/validation", code, body)
    check("client valid: exists, active, name matches", code == 200 and body["valid"])

    print("\n2. Loan application")
    application = {
        "external_reference": f"SIM-{RUN}-APPLY",
        "client_uid": CLIENT_UID,
        "client_name": CLIENT_NAME,
        "principal": PRINCIPAL,
        "submitted_on_date": LOAN_DATE,
    }
    code, body = ours("POST", "/loans", application)
    show("POST /loans", code, body)
    check("application created (201)", code == 201)
    loan_reference = body.get("loan_reference") if isinstance(body, dict) else None
    if not loan_reference:
        return summary()
    code, again = ours("POST", "/loans", application)
    check("same application again returns the same loan_reference (200)", code == 200 and again.get("loan_reference") == loan_reference)
    code, conflict = ours("POST", "/loans", {**application, "principal": PRINCIPAL + 1})
    check("same reference, different principal -> 409", code == 409)
    state = stage("application", loan_reference)
    check("Fineract status: Submitted and pending approval", state.get("status") == "Submitted and pending approval")

    print("\n3. Approval")
    code, body = ours("POST", f"/loans/{loan_reference}/approval", {"external_reference": f"SIM-{RUN}-APPROVE", "approved_on_date": LOAN_DATE})
    show("POST /loans/{ref}/approval", code, body)
    check("approved (201)", code == 201)
    state = stage("approval", loan_reference)
    check("Fineract status: Approved", state.get("status") == "Approved")

    print("\n4. Disbursement: 10 parallel requests, each with a different external_reference")
    results = parallel(
        10,
        lambda i: ours(
            "POST", f"/loans/{loan_reference}/disbursement", {"external_reference": f"SIM-{RUN}-DISBURSE-{i}", "disbursed_on_date": LOAN_DATE}
        ),
    )
    codes = sorted(code for code, _ in results)
    print(f"  -> status codes {codes}")
    winner = next((body for code, body in results if code == 201), None)
    if winner:
        show("winning disbursement", 201, winner)
    check("exactly one 201, the other nine 409", codes.count(201) == 1 and codes.count(409) == 9)
    state = stage("disbursement", loan_reference)
    disbursements = [t for t in state.get("transactions", []) if t[0] == "Disbursement"]
    check("Fineract: one disbursement, status Active", len(disbursements) == 1 and state.get("status") == "Active")
    check(f"Fineract principal disbursed is {PRINCIPAL}", state.get("principalDisbursed") == PRINCIPAL)

    for number, repayment_date in enumerate(REPAYMENT_DATES, start=1):
        print(f"\n{4 + number}. Partial repayment {number}: {PARTIAL_REPAYMENT} RWF on {repayment_date}")
        repayment = {"external_reference": f"SIM-{RUN}-REPAY-{number}", "amount": PARTIAL_REPAYMENT, "transaction_date": repayment_date}
        if number == 1:
            results = [ours("POST", f"/loans/{loan_reference}/repayments", repayment) for _ in range(4)]
            print(f"  same request sent 4 times -> {[code for code, _ in results]}")
            code, body = results[0]
            check("4 identical requests: one 201 then three 200 with the same body", [c for c, _ in results] == [201, 200, 200, 200] and all(b == body for _, b in results))
        else:
            code, body = ours("POST", f"/loans/{loan_reference}/repayments", repayment)
        show("POST /loans/{ref}/repayments", code, body)
        check(f"repayment {number} split by Fineract: principal + interest = amount", code in (200, 201) and body["principal_portion"] + body["interest_portion"] == PARTIAL_REPAYMENT)
        state = stage(f"repayment {number}", loan_reference)
        matching = [t for t in state.get("transactions", []) if t[1] == repayment["external_reference"]]
        check(f"Fineract has exactly one transaction for {repayment['external_reference']}", len(matching) == 1)
        if matching:
            check("our response carries Fineract's own portions", (matching[0][3], matching[0][4]) == (body["principal_portion"], body["interest_portion"]))

    print(f"\n8. Final payoff on {PAYOFF_DATE}")
    code, loan = ours("GET", f"/loans/{loan_reference}")
    payoff = loan["balances"]["total_outstanding"]
    print(f"  GET /loans/{{ref}} -> total_outstanding {payoff} (read live from Fineract)")
    code, body = ours("POST", f"/loans/{loan_reference}/repayments", {"external_reference": f"SIM-{RUN}-REPAY-1", "amount": payoff, "transaction_date": PAYOFF_DATE})
    check("reusing a repayment reference with a different amount -> 409", code == 409)
    code, body = ours("POST", f"/loans/{loan_reference}/repayments", {"external_reference": f"SIM-{RUN}-OVERPAY", "amount": payoff + 1, "transaction_date": PAYOFF_DATE})
    check("paying more than Fineract's outstanding balance -> 422", code == 422)
    code, body = ours("POST", f"/loans/{loan_reference}/repayments", {"external_reference": f"SIM-{RUN}-PAYOFF", "amount": payoff, "transaction_date": PAYOFF_DATE})
    show("POST /loans/{ref}/repayments", code, body)
    check("payoff accepted (201)", code == 201)
    state = stage("final payoff", loan_reference)
    check("Fineract status: Closed (obligations met)", state.get("status") == "Closed (obligations met)")
    check("Fineract total outstanding is 0", state.get("totalOutstanding") == 0)

    print("\n9. Our view of the loan")
    code, loan = ours("GET", f"/loans/{loan_reference}")
    print(json.dumps(loan, indent=2))
    check("GET /loans/{ref} reports Fineract's status", loan.get("status") == "Closed (obligations met)")

    print("\n10. No Fineract numeric ids in any response")
    leaked = find_keys(responses, FINERACT_ID_KEYS)
    print(f"  scanned {len(responses)} responses, keys found: {sorted(leaked) or 'none'}")
    check("no Fineract id keys in our responses", not leaked)

    return summary()


def summary() -> int:
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
