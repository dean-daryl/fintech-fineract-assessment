"""Part I: duplicate and concurrent disbursements and repayments against the running stack.

I1  the same disbursement sent 4 times in a row: one Fineract disbursement, one ERP entry, one operation.
I2a 10 simultaneous disbursements of one loan with the same external_reference.
I2b 10 simultaneous disbursements of one loan, each with a different external_reference.
I3  10 repayments with different references, each sent 3 times, all 30 fired at once on one loan.
    Every reference applies once, and Fineract's outstanding balance falls by exactly the amounts
    Fineract itself recorded (its transaction breakdown), not the amounts this script sent.

Every request in a parallel batch waits on a barrier so they leave together. Counts come from each
system directly: Fineract's loan transactions, the ERP's and the integration service's databases.
Every run uses new references, so it can be re-run on the same stack.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = base64.b64encode(
    f"{os.environ.get('FINERACT_API_USERNAME', 'integration-api')}:{os.environ.get('FINERACT_API_PASSWORD', '')}".encode()
).decode()
ERP_DB_PASSWORD = os.environ.get("ERP_DB_PASSWORD", "")
INTEGRATION_DB_PASSWORD = os.environ.get("INTEGRATION_DB_PASSWORD", "")
SELF_SIGNED = ssl._create_unverified_context()

RUN = f"{int(time.time()) % 1_000_000:06d}"
PREFIX = f"SIMC-{RUN}"
PRINCIPAL = 5_000_000
LOAN_DATE = "2026-04-01"
REPAYMENT_DATE = "2026-05-01"
REPAYMENT = 100_000
PARALLEL = 10
COPIES = 3
SETTLE_SECONDS = 120

checks: list[tuple[str, bool]] = []


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
    return call(method, f"{INTEGRATION_URL}{path}", body)


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def show(label: str, code: int, body: Any) -> None:
    print(f"  {label} -> {code} {json.dumps(body)}")


def psql(user: str, password: str, sql: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", f"PGPASSWORD={password}", "db", "psql", "-U", user, "-d", user, "-tAc", sql],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def service_log_count(reference_prefix: str, message: str) -> int:
    logs = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", "--since", "30m", "integration-service"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    count = 0
    for line in logs.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("message") == message and str(entry.get("external_reference", "")).startswith(reference_prefix):
            count += 1
    return count


def erp_entries(like: str) -> int:
    return int(psql("erp", ERP_DB_PASSWORD, f"SELECT count(*) FROM journal_entries WHERE external_reference LIKE '{like}'"))


def operations(operation_type: str, loan_reference: str) -> dict[str, int]:
    rows = psql(
        "integration",
        INTEGRATION_DB_PASSWORD,
        f"SELECT status, count(*) FROM operations WHERE operation_type = '{operation_type}' "
        f"AND payload->>'loan_reference' = '{loan_reference}' GROUP BY status ORDER BY status",
    )
    return {status: int(count) for status, count in (line.split("|") for line in rows.splitlines() if line)}


def fineract_loan(loan_reference: str) -> dict[str, Any]:
    code, loan = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}?associations=transactions", fineract=True)
    if code != 200:
        raise SystemExit(f"Fineract GET loan {loan_reference} -> {code} {loan}")
    summary = loan.get("summary") or {}
    return {
        "status": loan["status"]["value"],
        "principalOutstanding": int(summary.get("principalOutstanding") or 0),
        "interestOutstanding": int(summary.get("interestOutstanding") or 0),
        "totalOutstanding": int(summary.get("totalOutstanding") or 0),
        "transactions": [
            {
                "type": t["type"]["value"],
                "externalId": t.get("externalId"),
                "amount": int(t["amount"]),
                "principal": int(t.get("principalPortion") or 0),
                "interest": int(t.get("interestPortion") or 0),
            }
            for t in loan.get("transactions") or []
            if not t.get("manuallyReversed")
        ],
    }


def of_type(loan: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [t for t in loan["transactions"] if t["type"] == kind]


def print_loan(label: str, loan: dict[str, Any]) -> None:
    print(f"  Fineract {label}: status {loan['status']!r}, principalOutstanding {loan['principalOutstanding']}, "
          f"interestOutstanding {loan['interestOutstanding']}, totalOutstanding {loan['totalOutstanding']}")


def simultaneous(requests: list[tuple[str, dict[str, Any]]]) -> list[tuple[int, Any]]:
    barrier = threading.Barrier(len(requests))

    def send(request: tuple[str, dict[str, Any]]) -> tuple[int, Any]:
        barrier.wait()
        return ours("POST", *request)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(send, requests))


def settle(path: str, body: dict[str, Any]) -> tuple[int, Any]:
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        code, response = ours("POST", path, body)
        if code != 202 or time.monotonic() > deadline:
            return code, response
        ours("POST", "/admin/recover")
        time.sleep(2)


CLIENT_UID = f"SME-{RUN}"
CLIENT_NAME = f"Concurrency Traders {RUN} Ltd"


def onboard_client() -> None:
    code, body = ours(
        "POST",
        "/api/clients",
        {"external_reference": f"{PREFIX}-CLIENT", "client_uid": CLIENT_UID, "name": CLIENT_NAME, "activation_date": "2026-03-01"},
    )
    show("POST /api/clients", code, body)
    check("client created (201)", code == 201)


def approved_loan(name: str) -> str:
    client_uid, client_name = CLIENT_UID, CLIENT_NAME
    code, body = ours(
        "POST",
        "/api/loans",
        {
            "external_reference": f"{PREFIX}-{name}-APPLY",
            "client_uid": client_uid,
            "client_name": client_name,
            "principal": PRINCIPAL,
            "submitted_on_date": LOAN_DATE,
        },
    )
    check(f"loan {name}: application created (201)", code == 201)
    loan_reference: str = body["loan_reference"]
    code, _ = ours("POST", f"/api/loans/{loan_reference}/approval", {"external_reference": f"{PREFIX}-{name}-APPROVE", "approved_on_date": LOAN_DATE})
    check(f"loan {name}: approved (201)", code == 201)
    print(f"  loan {name} = {loan_reference}, principal {PRINCIPAL} RWF, approved")
    return loan_reference


def disbursement_counts(title: str, loan_reference: str, requests: int, erp_like: str) -> tuple[dict[str, Any], int, dict[str, int]]:
    loan = fineract_loan(loan_reference)
    disbursements = of_type(loan, "Disbursement")
    entries = erp_entries(erp_like)
    ops = operations("DISBURSE_LOAN", loan_reference)
    print(f"\n  {title}")
    print(f"    Requests received:          {requests}")
    print(f"    Fineract disbursements:     {len(disbursements)} {[t['externalId'] for t in disbursements]}")
    print(f"    ERP journal entries:        {entries}")
    print(f"    Integration transactions:   {ops.get('COMPLETED', 0)} completed (operations rows by status: {ops})")
    print_loan("loan", loan)
    return loan, entries, ops


def scenario_i1(loan_reference: str) -> None:
    print(f"\nI1. The same disbursement sent 4 times in a row (loan {loan_reference})")
    path = f"/api/loans/{loan_reference}/disbursement"
    body = {"external_reference": f"{PREFIX}-I1-DISBURSE", "disbursed_on_date": LOAN_DATE}
    results = [ours("POST", path, body) for _ in range(4)]
    print(f"  POST {path} {json.dumps(body)} x4 -> {[code for code, _ in results]}")
    show("first response", *results[0])
    check("one 201 then three 200", [code for code, _ in results] == [201, 200, 200, 200])
    check("the three replays return the original body", all(b == results[0][1] for _, b in results[1:]))
    loan, entries, ops = disbursement_counts("Counts", loan_reference, 4, f"{PREFIX}-I1-%")
    check("Fineract disbursements: 1", len(of_type(loan, "Disbursement")) == 1)
    check("ERP journal entries: 1", entries == 1)
    check("Integration transactions: 1 (one operations row, COMPLETED)", ops == {"COMPLETED": 1})


def scenario_i2a(loan_reference: str) -> None:
    print(f"\nI2a. {PARALLEL} simultaneous disbursements, same external_reference (loan {loan_reference})")
    path = f"/api/loans/{loan_reference}/disbursement"
    body = {"external_reference": f"{PREFIX}-I2A-DISBURSE", "disbursed_on_date": LOAN_DATE}
    results = simultaneous([(path, body)] * PARALLEL)
    codes = sorted(code for code, _ in results)
    print(f"  -> status codes {codes}")
    check(f"exactly one 201; the other {PARALLEL - 1} are 202 (in progress) or 200 (finished replay)", codes.count(201) == 1 and all(c in (200, 202) for c in codes if c != 201))
    check("no request was rejected or errored", all(c in (200, 201, 202) for c in codes))
    code, final = settle(path, body)
    show("same request after the burst", code, final)
    check("a replay after the burst returns 200 with the original result", code == 200 and final.get("operation_status") == "COMPLETED")
    loan, entries, ops = disbursement_counts("Counts", loan_reference, PARALLEL, f"{PREFIX}-I2A-%")
    check("Fineract disbursements: 1", len(of_type(loan, "Disbursement")) == 1)
    check("ERP journal entries: 1", entries == 1)
    check("Integration transactions: 1 (the unique (operation_type, external_reference) allows one row)", ops == {"COMPLETED": 1})


def scenario_i2b(loan_reference: str) -> None:
    print(f"\nI2b. {PARALLEL} simultaneous disbursements, each with a different external_reference (loan {loan_reference})")
    path = f"/api/loans/{loan_reference}/disbursement"
    requests = [(path, {"external_reference": f"{PREFIX}-I2B-DISBURSE-{i}", "disbursed_on_date": LOAN_DATE}) for i in range(PARALLEL)]
    results = simultaneous(requests)
    codes = sorted(code for code, _ in results)
    print(f"  -> status codes {codes}")
    winner = next((body for code, body in results if code == 201), None)
    loser = next((body for code, body in results if code == 409), None)
    if winner:
        show("winner", 201, winner)
    if loser:
        show("a loser", 409, loser)
    check(f"exactly one 201, the other {PARALLEL - 1} are 409", codes.count(201) == 1 and codes.count(409) == PARALLEL - 1)
    loan, entries, ops = disbursement_counts("Counts", loan_reference, PARALLEL, f"{PREFIX}-I2B-%")
    disbursements = of_type(loan, "Disbursement")
    check("Fineract disbursements: 1", len(disbursements) == 1)
    check("the Fineract disbursement carries the winner's reference", bool(winner) and [t["externalId"] for t in disbursements] == [winner["external_reference"]])
    check("ERP journal entries: 1", entries == 1)
    check(
        f"Integration transactions: 1 COMPLETED; the {PARALLEL - 1} rejected requests are recorded as FAILED and never reached Fineract",
        ops == {"COMPLETED": 1, "FAILED": PARALLEL - 1},
    )


def scenario_i3(loan_reference: str) -> None:
    print(f"\nI3. {PARALLEL} repayments with different references, each sent {COPIES} times, all {PARALLEL * COPIES} at once (loan {loan_reference})")
    before = fineract_loan(loan_reference)
    print_loan("before", before)
    path = f"/api/loans/{loan_reference}/repayments"
    references = [f"{PREFIX}-I3-REPAY-{i}" for i in range(PARALLEL)]
    bodies = {ref: {"external_reference": ref, "amount": REPAYMENT, "transaction_date": REPAYMENT_DATE} for ref in references}
    results = simultaneous([(path, bodies[ref]) for ref in references for _ in range(COPIES)])
    codes = sorted(code for code, _ in results)
    print(f"  -> status codes {codes}")
    check("no request was rejected or errored (only 200, 201 or 202)", all(c in (200, 201, 202) for c in codes))
    check(f"at most one 201 per reference ({codes.count(201)} in total)", codes.count(201) <= PARALLEL)

    print("\n  Each reference after the burst (replayed until finished):")
    final: dict[str, dict[str, Any]] = {}
    for ref in references:
        code, body = settle(path, bodies[ref])
        final[ref] = body
        print(f"    {ref}: {code} {body.get('operation_status')} principal {body.get('principal_portion')} interest {body.get('interest_portion')} erp {body.get('erp_transaction_id')}")
    check("every reference finished COMPLETED", all(b.get("operation_status") == "COMPLETED" for b in final.values()))

    after = fineract_loan(loan_reference)
    print_loan("after", after)
    applied = [t for t in of_type(after, "Repayment") if t["externalId"] in bodies]
    per_reference = {ref: sum(1 for t in applied if t["externalId"] == ref) for ref in references}
    print(f"  Fineract repayments per reference: {per_reference}")
    check(f"Fineract: each of the {PARALLEL} references applied exactly once", all(n == 1 for n in per_reference.values()))
    check(f"Fineract: no extra repayment from the {PARALLEL * (COPIES - 1)} duplicates", len(applied) == PARALLEL)

    recorded = sum(t["amount"] for t in applied)
    principal = sum(t["principal"] for t in applied)
    interest = sum(t["interest"] for t in applied)
    print(f"  Fineract's breakdown of the {len(applied)} repayments: amount {recorded}, principal {principal}, interest {interest}")
    print(f"  totalOutstanding     {before['totalOutstanding']} - {recorded} = {before['totalOutstanding'] - recorded}; Fineract says {after['totalOutstanding']}")
    print(f"  principalOutstanding {before['principalOutstanding']} - {principal} = {before['principalOutstanding'] - principal}; Fineract says {after['principalOutstanding']}")
    print(f"  interestOutstanding  {before['interestOutstanding']} - {interest} = {before['interestOutstanding'] - interest}; Fineract says {after['interestOutstanding']}")
    check("each repayment's principal + interest equals its amount (Fineract's split)", all(t["principal"] + t["interest"] == t["amount"] for t in applied))
    check("final totalOutstanding = initial - Fineract's recorded amounts", after["totalOutstanding"] == before["totalOutstanding"] - recorded)
    check("final principalOutstanding = initial - Fineract's principal portions", after["principalOutstanding"] == before["principalOutstanding"] - principal)
    check("final interestOutstanding = initial - Fineract's interest portions", after["interestOutstanding"] == before["interestOutstanding"] - interest)
    check("our responses carry Fineract's portions", all(
        (final[t["externalId"]].get("principal_portion"), final[t["externalId"]].get("interest_portion")) == (t["principal"], t["interest"]) for t in applied
    ))

    entries = erp_entries(f"{PREFIX}-I3-%")
    ops = operations("REPAY_LOAN", loan_reference)
    print(f"\n  Counts")
    print(f"    Requests received:          {PARALLEL * COPIES} ({PARALLEL} references x {COPIES})")
    print(f"    Fineract repayments:        {len(applied)}")
    print(f"    ERP journal entries:        {entries}")
    print(f"    Integration transactions:   {ops.get('COMPLETED', 0)} completed (operations rows by status: {ops})")
    retried = service_log_count(f"{PREFIX}-I3-", "Fineract attempt failed")
    print(f"    Fineract concurrency 409s retried by the service: {retried} (Fineract rolled each back, so each was resent)")
    check(f"ERP journal entries: {PARALLEL}", entries == PARALLEL)
    check(f"Integration transactions: {PARALLEL}, all COMPLETED", ops == {"COMPLETED": PARALLEL})


def main() -> int:
    print(f"Run {RUN}: Part I duplicates and concurrency, references prefixed {PREFIX}\n")
    print("Setup: one client and three approved loans (one disbursement per loan is the invariant, so each disbursement scenario needs its own loan)")
    onboard_client()
    loan_i1 = approved_loan("I1")
    loan_i2a = approved_loan("I2A")
    loan_i2b = approved_loan("I2B")
    scenario_i1(loan_i1)
    scenario_i2a(loan_i2a)
    scenario_i2b(loan_i2b)
    scenario_i3(loan_i1)
    return summary()


def summary() -> int:
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
