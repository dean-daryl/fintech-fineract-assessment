"""Part J: the five failure scenarios, each with the state before, during and after recovery.

1. ERP down after Fineract succeeded (disbursement): the mock-erp container is really stopped.
2. Fineract down (repayment): injected before any Fineract call.
3. Fineract times out after the repayment was booked: injected after the call returned.
4. ERP times out after the journal entry was written: injected after the call returned.
5. Crash between Fineract success and the ERP call: the integration service process really exits.

Faults are armed through /admin/faults (FAULT_INJECTION_ENABLED=true) and scoped to one external_reference.
State is read from three places every time: our operation (/admin/operations), Fineract's loan transactions
and the ERP (journal entry by reference, plus a row count straight from the ERP database).

Every run uses a new client and loan, so it can be re-run on the same stack.
Uses only the standard library so it runs on the host without a virtualenv.
"""

import base64
import http.client
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
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
SELF_SIGNED = ssl._create_unverified_context()

RUN = f"{int(time.time()) % 1_000_000:06d}"
CLIENT_UID = f"SME-{RUN}"
CLIENT_NAME = f"Failure Traders {RUN} Ltd"
PRINCIPAL = 5_000_000
REPAYMENT = 500_000
LOAN_DATE = "2026-04-01"

checks: list[tuple[str, bool]] = []


def call(
    method: str, url: str, body: dict[str, Any] | None = None, fineract: bool = False, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    if fineract:
        request.add_header("Authorization", f"Basic {FINERACT_AUTH}")
        request.add_header("Fineract-Platform-TenantId", "default")
    try:
        with urllib.request.urlopen(request, context=SELF_SIGNED if fineract else None, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException, TimeoutError) as exc:
        return 0, {"connection_error": repr(getattr(exc, "reason", exc))}


def ours(method: str, path: str, body: dict[str, Any] | None = None, correlation_id: str | None = None) -> tuple[int, Any]:
    headers = {"X-Correlation-ID": correlation_id} if correlation_id else None
    return call(method, f"{INTEGRATION_URL}{path}", body, headers=headers)


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def show(label: str, code: int, body: Any) -> None:
    print(f"  {label} -> {code} {json.dumps(body)}")


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def restart_count() -> int:
    container = compose("ps", "-q", "integration-service").strip()
    inspect = subprocess.run(["docker", "inspect", "-f", "{{.RestartCount}}", container], capture_output=True, text=True, check=True)
    return int(inspect.stdout.strip())


def wait_until_up(url: str, deadline_seconds: int) -> float:
    started = time.monotonic()
    while time.monotonic() - started < deadline_seconds:
        if call("GET", url)[0] == 200:
            return time.monotonic() - started
        time.sleep(1)
    raise SystemExit(f"{url} did not come back within {deadline_seconds}s")


def operation(reference: str) -> dict[str, Any] | None:
    code, rows = ours("GET", f"/admin/operations/{reference}")
    if code != 200 or not rows:
        return None
    row = rows[0]
    return {key: row[key] for key in ("operation_type", "status", "attempts", "last_error", "erp_transaction_id")}


def fineract_transactions(loan_reference: str, reference: str) -> list[tuple[str, int, int, int]]:
    code, loan = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}?associations=transactions", fineract=True)
    if code != 200:
        return []
    return [
        (t["type"]["value"], int(t["amount"]), int(t.get("principalPortion") or 0), int(t.get("interestPortion") or 0))
        for t in loan.get("transactions") or []
        if t.get("externalId") == reference and not t.get("manuallyReversed")
    ]


def erp_entry(reference: str) -> tuple[int, Any]:
    return call("GET", f"{ERP_URL}/api/journal-entries/{reference}")


def erp_rows(reference: str) -> int:
    out = compose(
        "exec", "-T", "-e", f"PGPASSWORD={ERP_DB_PASSWORD}", "db",
        "psql", "-U", "erp", "-d", "erp", "-tAc", f"SELECT count(*) FROM journal_entries WHERE external_reference = '{reference}'",
    )
    return int(out.strip())


def erp_balance(code: str) -> int:
    return int(call("GET", f"{ERP_URL}/api/accounts/{code}/balance")[1]["balance"])


def lines_of(entry: Any) -> list[tuple[str, int, int]]:
    return [(line["account_code"], line["debit"], line["credit"]) for line in entry["lines"]]


def expected_lines(transaction: tuple[str, int, int, int]) -> list[tuple[str, int, int]]:
    kind, amount, principal, interest = transaction
    if kind == "Disbursement":
        return [("1200", amount, 0), ("1100", 0, amount)]
    lines = [("1100", amount, 0)]
    if principal:
        lines.append(("1200", 0, principal))
    if interest:
        lines.append(("4100", 0, interest))
    return lines


def state(label: str, loan_reference: str, reference: str) -> dict[str, Any]:
    op = operation(reference)
    transactions = fineract_transactions(loan_reference, reference)
    code, entry = erp_entry(reference)
    rows = erp_rows(reference)
    print(f"  {label}:")
    print(f"    operation          {json.dumps(op)}")
    print(f"    Fineract           {len(transactions)} transaction(s) with externalId {reference}: {transactions}")
    erp_view = f"{entry['id']} {lines_of(entry)}" if code == 200 else f"HTTP {code}"
    print(f"    ERP                GET -> {erp_view}; rows in journal_entries: {rows}")
    return {"operation": op, "transactions": transactions, "erp_code": code, "erp_entry": entry, "erp_rows": rows}


def arm(point: str, reference: str, times: int) -> None:
    code, body = ours("POST", "/admin/faults", {"point": point, "external_reference": reference, "times": times})
    if code != 201:
        raise SystemExit(f"could not arm {point}: {code} {body} (is FAULT_INJECTION_ENABLED=true?)")
    print(f"  armed {point} x{times} for {reference}")


def recover_until_finished(reference: str, deadline_seconds: int, call_recover: bool = True) -> tuple[dict[str, Any] | None, float]:
    started = time.monotonic()
    op = operation(reference)
    while time.monotonic() - started < deadline_seconds:
        if call_recover:
            ours("POST", "/admin/recover")
        op = operation(reference)
        if op and op["status"] in ("COMPLETED", "FAILED", "NEEDS_RECONCILIATION"):
            break
        time.sleep(2)
    return op, time.monotonic() - started


def service_log(correlation_id: str, since: str) -> list[dict[str, Any]]:
    entries = []
    for line in compose("logs", "--no-log-prefix", "--since", since, "integration-service").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("correlation_id") == correlation_id:
            entries.append(entry)
    return entries


def print_log(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        print(f"    {entry['timestamp'][11:23]} {entry['level']:<8} {entry['message']}")


def finished_correctly(after: dict[str, Any], kind: str) -> None:
    op, transactions = after["operation"], after["transactions"]
    check("after: operation COMPLETED with an erp_transaction_id", bool(op and op["status"] == "COMPLETED" and op["erp_transaction_id"]))
    check(f"after: Fineract has exactly one {kind} with this externalId", len(transactions) == 1 and transactions[0][0] == kind)
    check("after: ERP has exactly one journal entry for this reference (DB row count 1)", after["erp_rows"] == 1)
    if after["erp_code"] == 200 and transactions:
        check("after: the ERP entry carries Fineract's own numbers", lines_of(after["erp_entry"]) == expected_lines(transactions[0]))
        check("after: the operation names that ERP entry", bool(op and op["erp_transaction_id"] == after["erp_entry"]["id"]))


def setup() -> str:
    print("Setup: onboard a client, apply for a loan and approve it")
    client = {"external_reference": f"SIMF-{RUN}-CLIENT", "client_uid": CLIENT_UID, "name": CLIENT_NAME, "activation_date": "2026-03-01"}
    code, body = ours("POST", "/api/clients", client)
    check("client created (201)", code == 201)
    application = {
        "external_reference": f"SIMF-{RUN}-APPLY",
        "client_uid": CLIENT_UID,
        "client_name": CLIENT_NAME,
        "principal": PRINCIPAL,
        "submitted_on_date": LOAN_DATE,
    }
    code, body = ours("POST", "/api/loans", application)
    check("loan application created (201)", code == 201)
    loan_reference: str = body["loan_reference"]
    code, _ = ours("POST", f"/api/loans/{loan_reference}/approval", {"external_reference": f"SIMF-{RUN}-APPROVE", "approved_on_date": LOAN_DATE})
    check("loan approved (201)", code == 201)
    print(f"  loan {loan_reference}, principal {PRINCIPAL} RWF")
    return loan_reference


def scenario_erp_down(loan_reference: str) -> None:
    reference = f"SIMF-{RUN}-S1-DISBURSE"
    request = {"external_reference": reference, "disbursed_on_date": LOAN_DATE}
    path = f"/api/loans/{loan_reference}/disbursement"
    print("\nScenario 1: ERP down after Fineract success (disbursement)")
    receivable_before = erp_balance("1200")
    state("before", loan_reference, reference)

    print("  docker compose stop mock-erp")
    compose("stop", "mock-erp")
    code, body = ours("POST", path, request)
    show("POST disbursement", code, body)
    check("caller gets 202 with operation_status ERP_PENDING and Fineract's result", code == 202 and body.get("operation_status") == "ERP_PENDING" and body.get("amount") == PRINCIPAL)
    during = state("during (ERP stopped)", loan_reference, reference)
    check("during: Fineract disbursed once; ERP has nothing", len(during["transactions"]) == 1 and during["erp_rows"] == 0)
    check("during: the operation waits in ERP_PENDING with the failure recorded", bool(during["operation"] and during["operation"]["status"] == "ERP_PENDING" and during["operation"]["last_error"]))
    code, body = ours("POST", path, request)
    show("same request again while the ERP is down", code, body)
    check("replay while pending: 202, Fineract still has one disbursement", code == 202 and len(fineract_transactions(loan_reference, reference)) == 1)
    ours("POST", "/admin/recover")
    op = operation(reference)
    print(f"  POST /admin/recover while the ERP is down -> operation {json.dumps(op)}")
    check("recovery while the ERP is down keeps it ERP_PENDING", bool(op and op["status"] == "ERP_PENDING"))

    print("  docker compose start mock-erp")
    compose("start", "mock-erp")
    print(f"  ERP healthy again after {wait_until_up(f'{ERP_URL}/health', 60):.0f}s")
    op, waited = recover_until_finished(reference, 120)
    print(f"  POST /admin/recover until finished: {waited:.0f}s (deferred retries back off 5s, 10s, ...)")
    after = state("after recovery", loan_reference, reference)
    finished_correctly(after, "Disbursement")
    moved = erp_balance("1200") - receivable_before
    check(f"after: Loan Receivable (1200) moved by {PRINCIPAL} exactly once (moved {moved})", moved == PRINCIPAL)
    code, body = ours("POST", path, request)
    check("replay after recovery: 200 with the same erp_transaction_id, no new entry", code == 200 and body.get("erp_transaction_id") == after["erp_entry"].get("id") and erp_rows(reference) == 1)


def scenario_fineract_down(loan_reference: str) -> None:
    reference = f"SIMF-{RUN}-S2-REPAY"
    request = {"external_reference": reference, "amount": REPAYMENT, "transaction_date": "2026-05-01"}
    path = f"/api/loans/{loan_reference}/repayments"
    print("\nScenario 2: Fineract down (repayment)")
    state("before", loan_reference, reference)
    arm("FINERACT_DOWN", reference, 1000)
    code, body = ours("POST", path, request)
    show("POST repayment", code, body)
    check("caller gets 202 with operation_status RECEIVED", code == 202 and body.get("operation_status") == "RECEIVED")
    during = state("during (Fineract down)", loan_reference, reference)
    check("during: the request is persisted (RECEIVED, attempts 1, error recorded)", bool(during["operation"] and during["operation"]["status"] == "RECEIVED" and during["operation"]["attempts"] == 1 and during["operation"]["last_error"]))
    check("during: nothing reached Fineract or the ERP", not during["transactions"] and during["erp_rows"] == 0)
    code, body = ours("POST", path, request)
    show("same request again (safe retry)", code, body)
    check("safe to retry: same reference and payload -> 202, still nothing executed", code == 202 and not fineract_transactions(loan_reference, reference))
    code, body = ours("POST", path, {**request, "amount": REPAYMENT + 1})
    check("same reference with a different amount -> 409", code == 409)
    time.sleep(6)
    ours("POST", "/admin/recover")
    op = operation(reference)
    print(f"  POST /admin/recover while Fineract is down -> operation {json.dumps(op)}")
    check("recovery while Fineract is down: retried (attempts 2), still RECEIVED", bool(op and op["status"] == "RECEIVED" and op["attempts"] == 2))

    ours("DELETE", "/admin/faults")
    print("  fault cleared: Fineract reachable again")
    op, waited = recover_until_finished(reference, 120)
    print(f"  POST /admin/recover until finished: {waited:.0f}s")
    finished_correctly(state("after recovery", loan_reference, reference), "Repayment")


def scenario_fineract_timeout(loan_reference: str) -> None:
    reference = f"SIMF-{RUN}-S3-REPAY"
    request = {"external_reference": reference, "amount": REPAYMENT, "transaction_date": "2026-06-01"}
    correlation_id = str(uuid.uuid4())
    print("\nScenario 3: Fineract times out after the repayment was sent")
    state("before", loan_reference, reference)
    arm("FINERACT_TIMEOUT_AFTER_SEND", reference, 1)
    since = datetime.now(UTC).isoformat()
    code, body = ours("POST", f"/api/loans/{loan_reference}/repayments", request, correlation_id)
    show("POST repayment", code, body)
    entries = service_log(correlation_id, since)
    print(f"  during: the service log of this request (correlation_id {correlation_id}):")
    print_log(entries)
    sends = [e for e in entries if "command=repayment" in e["message"]]
    lookups = [e for e in entries if f"transactions/external-id/{reference}" in e["message"]]
    check("the repayment was sent to Fineract once and the response was lost", len(sends) == 1 and any("Fault fired: FINERACT_TIMEOUT_AFTER_SEND" in e["message"] for e in entries))
    check("the next step was a lookup by externalId, not a resend", len(lookups) >= 1 and entries.index(lookups[-1]) > entries.index(sends[0]))
    check("the caller still gets 201 with Fineract's split", code == 201 and body["principal_portion"] + body["interest_portion"] == REPAYMENT)
    finished_correctly(state("after", loan_reference, reference), "Repayment")


def scenario_erp_timeout(loan_reference: str) -> None:
    reference = f"SIMF-{RUN}-S4-REPAY"
    request = {"external_reference": reference, "amount": REPAYMENT, "transaction_date": "2026-07-01"}
    path = f"/api/loans/{loan_reference}/repayments"
    correlation_id = str(uuid.uuid4())
    print("\nScenario 4: ERP times out after the journal entry was written")
    bank_before = erp_balance("1100")
    state("before", loan_reference, reference)
    arm("ERP_TIMEOUT_AFTER_WRITE", reference, 1)
    since = datetime.now(UTC).isoformat()
    code, body = ours("POST", path, request, correlation_id)
    show("POST repayment", code, body)
    entries = service_log(correlation_id, since)
    print(f"  during: the service log of this request (correlation_id {correlation_id}):")
    print_log(entries)
    posts = [e for e in entries if "POST http://mock-erp:8000/api/journal-entries" in e["message"]]
    lookups = [e for e in entries if f"GET http://mock-erp:8000/api/journal-entries/{reference}" in e["message"]]
    check("the entry was posted once and the response was lost", len(posts) == 1 and any("Fault fired: ERP_TIMEOUT_AFTER_WRITE" in e["message"] for e in entries))
    check("the retry looked the entry up instead of posting again", len(lookups) >= 1 and entries.index(lookups[-1]) > entries.index(posts[0]))
    after = state("after", loan_reference, reference)
    finished_correctly(after, "Repayment")
    moved = erp_balance("1100") - bank_before
    check(f"Cash at Bank (1100) moved by {REPAYMENT} exactly once (moved {moved})", moved == REPAYMENT)

    entry = after["erp_entry"]
    resend = {key: entry[key] for key in ("external_reference", "entry_date", "description", "currency")}
    resend["lines"] = [{"account_code": a, "debit": d, "credit": c} for a, d, c in lines_of(entry)]
    erp_code, erp_body = call("POST", f"{ERP_URL}/api/journal-entries", resend)
    print(f"  a blind resend of the same entry straight to the ERP -> {erp_code} {erp_body.get('id')}")
    code, body = ours("POST", path, request)
    print(f"  the partner retries the same request -> {code} erp_transaction_id {body.get('erp_transaction_id')}")
    check("retries return the same entry: ERP 200 and our 200, still one row", erp_code == 200 and erp_body.get("id") == entry["id"] and code == 200 and body.get("erp_transaction_id") == entry["id"] and erp_rows(reference) == 1)


def scenario_crash(loan_reference: str) -> None:
    reference = f"SIMF-{RUN}-S5-REPAY"
    request = {"external_reference": reference, "amount": REPAYMENT, "transaction_date": "2026-08-01"}
    print("\nScenario 5: crash between Fineract success and the ERP call")
    restarts_before = restart_count()
    state("before", loan_reference, reference)
    arm("CRASH_BEFORE_ERP", reference, 1)
    code, body = ours("POST", f"/api/loans/{loan_reference}/repayments", request)
    show("POST repayment", code, body)
    check("the caller's connection drops: the process died mid-request", code == 0)
    print(f"  integration service back after {wait_until_up(f'{INTEGRATION_URL}/health', 120):.0f}s (restart: unless-stopped)")
    restarts = restart_count()
    check(f"the container really restarted (RestartCount {restarts_before} -> {restarts})", restarts == restarts_before + 1)
    during = state("during (right after the restart)", loan_reference, reference)
    check("during: Fineract booked the repayment; the ERP has nothing yet", len(during["transactions"]) == 1 and during["erp_rows"] == 0)
    check("during: the saved state says ERP_PENDING (persisted before the crash)", bool(during["operation"] and during["operation"]["status"] == "ERP_PENDING"))
    op, waited = recover_until_finished(reference, 180, call_recover=False)
    print(f"  no manual action: the in-process recovery sweep finished it after {waited:.0f}s (once the 60s lease expired)")
    finished_correctly(state("after recovery", loan_reference, reference), "Repayment")


def main() -> int:
    print(f"Run {RUN}: Part J failure scenarios\n")
    code, _ = ours("GET", "/admin/faults")
    if code != 200:
        print("Fault injection is disabled: set FAULT_INJECTION_ENABLED=true in .env and recreate integration-service")
        return 1
    ours("DELETE", "/admin/faults")
    loan_reference = setup()
    try:
        scenario_erp_down(loan_reference)
        scenario_fineract_down(loan_reference)
        scenario_fineract_timeout(loan_reference)
        scenario_erp_timeout(loan_reference)
        scenario_crash(loan_reference)
    finally:
        compose("start", "mock-erp")
        ours("DELETE", "/admin/faults")
    return summary()


def summary() -> int:
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
