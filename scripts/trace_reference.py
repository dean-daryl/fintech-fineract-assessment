"""Answers "What happened to FIN-2026-000001?" from one API call and the service's JSON logs.

Setup uses fixed references, so a second run replays every step instead of creating anything:
client SME-900001, a loan, its approval and disbursement, then repayment FIN-2026-000001.
The investigation then uses only what an engineer has: GET /api/transactions/{ref} and
`docker compose logs integration-service`, filtered by external_reference and then by correlation_id.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from integration_auth import integration_headers

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
REFERENCE = "FIN-2026-000001"
CLIENT_UID = "SME-900001"
CLIENT_NAME = "Observability Traders Ltd"
LOG_FIELDS = ("timestamp", "level", "message", "correlation_id", "external_reference", "client_uid", "loan_reference",
              "fineract_transaction_id", "erp_transaction_id", "status")


def call(method: str, path: str, body: dict[str, Any] | None = None, correlation_id: str | None = None) -> tuple[int, Any]:
    url = f"{INTEGRATION_URL}{path}"
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    for name, value in integration_headers(url).items():
        request.add_header(name, value)
    if correlation_id:
        request.add_header("X-Correlation-ID", correlation_id)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def service_logs() -> list[dict[str, Any]]:
    out = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", "integration-service"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    entries = []
    for line in out.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def show_log(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        print("  " + json.dumps({key: entry[key] for key in LOG_FIELDS if key in entry}))


def setup() -> str:
    print("Setup (fixed references; a re-run replays each step)")
    steps: list[tuple[str, dict[str, Any]]] = [
        ("/api/clients", {"external_reference": "OBS-CLIENT", "client_uid": CLIENT_UID, "name": CLIENT_NAME, "activation_date": "2026-03-01"}),
        ("/api/loans", {"external_reference": "OBS-APPLY", "client_uid": CLIENT_UID, "client_name": CLIENT_NAME,
                        "principal": 5_000_000, "submitted_on_date": "2026-04-01"}),
    ]
    loan_reference = ""
    for path, body in steps:
        code, response = call("POST", path, body)
        print(f"  POST {path} {body['external_reference']} -> {code} {response.get('operation_status')}")
        loan_reference = response.get("loan_reference", loan_reference)
    for path, body in (
        (f"/api/loans/{loan_reference}/approval", {"external_reference": "OBS-APPROVE", "approved_on_date": "2026-04-01"}),
        (f"/api/loans/{loan_reference}/disbursement", {"external_reference": "OBS-DISBURSE", "disbursed_on_date": "2026-04-01"}),
    ):
        code, response = call("POST", path, body)
        print(f"  POST {path} {body['external_reference']} -> {code} {response.get('operation_status')}")
    return loan_reference


def main() -> int:
    loan_reference = setup()
    correlation_id = f"corr-{uuid.uuid4()}"
    body = {"external_reference": REFERENCE, "amount": 500_000, "transaction_date": "2026-05-01"}
    code, response = call("POST", f"/api/loans/{loan_reference}/repayments", body, correlation_id)
    print(f"\nThe partner's request: POST /api/loans/{loan_reference}/repayments {json.dumps(body)}")
    print(f"  X-Correlation-ID: {correlation_id}")
    print(f"  -> {code} {json.dumps(response)}")

    print(f"\nWhat happened to {REFERENCE}?")
    print(f"\n1. One API call: GET /api/transactions/{REFERENCE}")
    code, transaction = call("GET", f"/api/transactions/{REFERENCE}")
    print(f"  -> {code}")
    print("  " + json.dumps(transaction, indent=2).replace("\n", "\n  "))

    entries = service_logs()
    by_reference = [e for e in entries if e.get("external_reference") == REFERENCE]
    print(f"\n2. The logs for this reference: docker compose logs integration-service | jq 'select(.external_reference == \"{REFERENCE}\")'")
    print(f"  {len(by_reference)} lines, from every request and recovery pass that touched it:")
    show_log(by_reference)

    correlation_ids = sorted({e["correlation_id"] for e in by_reference if e.get("correlation_id")})
    print(f"\n3. The full trail of each request: jq 'select(.correlation_id == \"...\")', including the calls to Fineract and the ERP")
    for cid in correlation_ids:
        print(f"\n  correlation_id {cid}")
        show_log([e for e in entries if e.get("correlation_id") == cid])

    ok = code == 200 and transaction.get("operation_status") == "COMPLETED" and bool(by_reference)
    fields = set().union(*(e.keys() for e in by_reference)) if by_reference else set()
    required = {"correlation_id", "external_reference", "loan_reference", "fineract_transaction_id", "erp_transaction_id", "status", "timestamp"}
    print(f"\nlog fields seen for {REFERENCE}: {sorted(fields & set(LOG_FIELDS))}")
    missing = sorted(required - fields)
    print(f"required fields missing: {missing or 'none'}")
    print("PASS" if ok and not missing else "FAIL")
    return 0 if ok and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
