"""Sends the same ERP account 4 times and the same sync request 4 times, in parallel, then counts the results in each system.

Expected: one ERP account, one Fineract GL account, one sync row (SYNCED), and one sync operation: one 201, the
other three 202 (still running) or 200 (finished), and a later replay returns 200 with the 201's body. Re-running
changes nothing in the ERP or Fineract; each run uses a new sync reference.
Uses only the standard library so it runs on the host without a virtualenv.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = base64.b64encode(
    f"{os.environ.get('FINERACT_API_USERNAME', 'mifos')}:{os.environ.get('FINERACT_API_PASSWORD', 'password')}".encode()
).decode()
SELF_SIGNED = ssl._create_unverified_context()

ACCOUNT = {"code": "1200", "name": "Loan Receivable", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1200"}
TIMES = 4
SYNC_REFERENCE = f"SIM-GL-SYNC-{int(time.time()) % 1_000_000:06d}"


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


def parallel(times: int, fn: Any) -> list[tuple[int, Any]]:
    with ThreadPoolExecutor(max_workers=times) as pool:
        return list(pool.map(lambda _: fn(), range(times)))


def main() -> int:
    reference = ACCOUNT["external_reference"]
    print(f"Account: {json.dumps(ACCOUNT)}\n")

    print(f"POST {ERP_URL}/api/accounts x{TIMES} (parallel)")
    for code, _ in parallel(TIMES, lambda: call("POST", f"{ERP_URL}/api/accounts", ACCOUNT)):
        print(f"  -> {code}")

    sync = {"external_reference": SYNC_REFERENCE}
    print(f"\nPOST {INTEGRATION_URL}/api/gl-accounts/sync x{TIMES} (parallel) {json.dumps(sync)}")
    results = parallel(TIMES, lambda: call("POST", f"{INTEGRATION_URL}/api/gl-accounts/sync", sync))
    for code, body in results:
        mine = [row for row in body.get("accounts", []) if row["external_reference"] == reference]
        print(f"  -> {code} operation_status={body.get('operation_status')} {json.dumps(mine)}")
    codes = sorted(code for code, _ in results)
    first = next((body for code, body in results if code == 201), None)
    replay_code, replay = call("POST", f"{INTEGRATION_URL}/api/gl-accounts/sync", sync)
    print(f"\nPOST {INTEGRATION_URL}/api/gl-accounts/sync again, same reference -> {replay_code} operation_status={replay.get('operation_status')}")

    _, erp_accounts = call("GET", f"{ERP_URL}/api/accounts")
    _, fineract_accounts = call("GET", f"{FINERACT_URL}/glaccounts", fineract=True)
    _, sync_rows = call("GET", f"{INTEGRATION_URL}/api/gl-accounts")

    counts = {
        "ERP accounts": sum(1 for a in erp_accounts if a["external_reference"] == reference),
        "Fineract GL accounts": sum(1 for a in fineract_accounts if a.get("externalReference") == reference),
        "integration sync rows": sum(1 for r in sync_rows if r["external_reference"] == reference),
    }
    statuses = [r["status"] for r in sync_rows if r["external_reference"] == reference]

    print(f"\nResult for {reference}:")
    for name, count in counts.items():
        print(f"  {name:<24} {count}")
    print(f"  {'sync status':<24} {', '.join(statuses)}")

    counts_ok = all(count == 1 for count in counts.values()) and statuses == ["SYNCED"]
    print(f"\nExpected 1 / 1 / 1 and SYNCED: {'PASS' if counts_ok else 'FAIL'}")
    sync_ok = codes.count(201) == 1 and all(code in (200, 202) for code in codes if code != 201)
    print(f"Expected one 201, the others 202 or 200 (got {codes}): {'PASS' if sync_ok else 'FAIL'}")
    replay_ok = replay_code == 200 and replay == first
    print(f"Expected the replay to be 200 with the 201's body: {'PASS' if replay_ok else 'FAIL'}")
    return 0 if counts_ok and sync_ok and replay_ok else 1


if __name__ == "__main__":
    sys.exit(main())
