"""Demonstrates the Fineract GL account patch directly against Fineract (no integration service).

Behaviours from the brief, plus the two code paths that only show up under load or misuse:
  1. externalReference is stored and unique
  2. same reference + identical payload returns the existing account
  3. same reference + different payload is rejected with 409
  4. the endpoint is protected by Fineract's permission model (SYNC_GLACCOUNT)
  5. parallel identical syncs create one account (unique constraint + Fineract command retry)
  6. stock POST /glaccounts rejects externalReference

Each run uses fresh references, so it can be re-run. Standard library only.
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

FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
ADMIN = (os.environ.get("FINERACT_API_USERNAME", "mifos"), os.environ.get("FINERACT_API_PASSWORD", "password"))
DEMO_ROLE = "GL Sync Demo"
DEMO_USER = "gl-sync-demo"
DEMO_PASSWORD = "QwE!5rTy#9uP0"
SELF_SIGNED = ssl._create_unverified_context()
PARALLEL = 10

failures: list[str] = []


def call(method: str, path: str, body: Any = None, auth: tuple[str, str] = ADMIN) -> tuple[int, Any]:
    request = urllib.request.Request(
        FINERACT_URL + path, method=method, data=json.dumps(body).encode() if body is not None else None
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Fineract-Platform-TenantId", "default")
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(request, context=SELF_SIGNED, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def check(label: str, condition: bool, detail: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}: {detail}")
    if not condition:
        failures.append(label)


def error_code(body: Any) -> str:
    return body.get("userMessageGlobalisationCode", "") if isinstance(body, dict) else str(body)


def gl_account(reference: str, gl_code: str, name: str = "Loan Receivable") -> dict[str, Any]:
    return {"externalReference": reference, "glCode": gl_code, "name": name, "type": 1, "usage": 1, "manualEntriesAllowed": True}


def demo_user_without_sync_permission() -> tuple[str, str]:
    _, roles = call("GET", "/roles")
    role = next((r for r in roles if r["name"] == DEMO_ROLE), None)
    if role is None:
        _, created = call("POST", "/roles", {"name": DEMO_ROLE, "description": "GL sync permission demo"})
        role_id = created["resourceId"]
    else:
        role_id = role["id"]
    call("PUT", f"/roles/{role_id}/permissions", {"permissions": {"READ_GLACCOUNT": True, "CREATE_GLACCOUNT": True, "SYNC_GLACCOUNT": False}})

    _, users = call("GET", "/users")
    if not any(u["username"] == DEMO_USER for u in users):
        status, body = call("POST", "/users", {
            "username": DEMO_USER, "firstname": "GL", "lastname": "Demo", "email": "gl-demo@example.com",
            "officeId": 1, "roles": [role_id], "sendPasswordToEmail": False,
            "password": DEMO_PASSWORD, "repeatPassword": DEMO_PASSWORD, "passwordNeverExpires": True,
        })
        if status != 200:
            raise SystemExit(f"could not create demo user: {status} {body}")
    return role_id, DEMO_USER


def main() -> int:
    run = str(int(time.time()))
    reference, gl_code = f"GL-DEMO-{run}", f"D{run}"

    print(f"Fineract: {FINERACT_URL}\nRun id: {run}\n")

    print("1. externalReference is stored and unique")
    status, created = call("POST", "/glaccounts/sync", gl_account(reference, gl_code))
    check("first sync creates the account", status == 200, f"HTTP {status}, resourceId={created.get('resourceId')}")
    account_id = created.get("resourceId")
    status, stored = call("GET", f"/glaccounts/external-reference/{reference}")
    check("lookup by reference returns it", status == 200 and stored.get("externalReference") == reference,
          f"HTTP {status}, id={stored.get('id')}, externalReference={stored.get('externalReference')}")
    status, body = call("GET", f"/glaccounts/external-reference/{reference}-unknown")
    check("unknown reference is 404", status == 404, f"HTTP {status}, {error_code(body)}")

    print("\n2. same reference + identical payload returns the existing account")
    status, repeated = call("POST", "/glaccounts/sync", gl_account(reference, gl_code))
    check("repeat returns the same id", status == 200 and repeated.get("resourceId") == account_id,
          f"HTTP {status}, resourceId={repeated.get('resourceId')} (first was {account_id})")
    _, all_accounts = call("GET", "/glaccounts")
    count = sum(1 for a in all_accounts if a.get("externalReference") == reference)
    check("still one account with this reference", count == 1, f"count={count}")

    print("\n3. same reference + different payload is rejected with 409")
    status, body = call("POST", "/glaccounts/sync", gl_account(reference, gl_code, name="Renamed"))
    check("changed name is 409", status == 409, f"HTTP {status}, {error_code(body)}")
    status, body = call("POST", "/glaccounts/sync", gl_account(f"{reference}-other", gl_code))
    check("new reference with a taken glCode is 409", status == 409, f"HTTP {status}, {error_code(body)}")
    _, stored = call("GET", f"/glaccounts/external-reference/{reference}")
    check("stored account unchanged", stored.get("name") == "Loan Receivable", f"name={stored.get('name')}")

    print("\n4. protected by Fineract's permission model (SYNC_GLACCOUNT)")
    role_id, username = demo_user_without_sync_permission()
    user = (username, DEMO_PASSWORD)
    status, body = call("POST", "/glaccounts/sync", gl_account(f"{reference}-perm", f"{gl_code}P"), auth=user)
    check("user without SYNC_GLACCOUNT is refused", status == 403, f"HTTP {status}, {error_code(body)}")
    call("PUT", f"/roles/{role_id}/permissions", {"permissions": {"SYNC_GLACCOUNT": True}})
    status, body = call("POST", "/glaccounts/sync", gl_account(f"{reference}-perm", f"{gl_code}P"), auth=user)
    check("same user after granting SYNC_GLACCOUNT", status == 200, f"HTTP {status}, resourceId={body.get('resourceId')}")
    call("PUT", f"/roles/{role_id}/permissions", {"permissions": {"SYNC_GLACCOUNT": False}})

    print(f"\n5. {PARALLEL} parallel identical syncs create one account")
    parallel_reference, parallel_code = f"{reference}-par", f"{gl_code}X"
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        results = list(pool.map(lambda _: call("POST", "/glaccounts/sync", gl_account(parallel_reference, parallel_code)), range(PARALLEL)))
    statuses = sorted(status for status, _ in results)
    ids = {body.get("resourceId") for status, body in results if status == 200}
    check("every request succeeds", statuses == [200] * PARALLEL, f"statuses={statuses}")
    check("all return the same account", len(ids) == 1, f"resourceIds={sorted(ids)}")
    _, all_accounts = call("GET", "/glaccounts")
    count = sum(1 for a in all_accounts if a.get("externalReference") == parallel_reference)
    check("one account in Fineract", count == 1, f"count={count}")

    print("\n6. stock POST /glaccounts rejects externalReference")
    status, body = call("POST", "/glaccounts", gl_account(f"{reference}-stock", f"{gl_code}S"))
    errors = body.get("errors", [{}]) if isinstance(body, dict) else [{}]
    check("stock create with externalReference is 400", status == 400, f"HTTP {status}, {errors[0].get('userMessageGlobalisationCode')}")

    print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
