"""Part L: seed every GL master-data outcome, show the report, then run the GL sync and report again.

Seeded with codes and references used by no other run:
  MATCHED        ERP account, synced through the integration API
  RENAMED        ERP account, synced, then renamed in Fineract with the stock PUT /glaccounts/{id}  -> DETAILS_MISMATCH
  NOT-SYNCED     ERP account created after the sync                                                -> MISSING_IN_FINERACT
  BRANCH         Fineract GL created with the stock POST /glaccounts (no external reference)       -> MISSING_IN_ERP
  ORPHAN         Fineract GL synced straight in Fineract with a reference the ERP never had        -> MISSING_IN_ERP
  CLASH          Fineract GL created with the stock POST, then an ERP account with the same code   -> CODE_CONFLICT

The direct Fineract calls use the admin user and stand in for a back-office user working outside the integration.
Uses only the standard library.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from integration_auth import integration_headers

INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = base64.b64encode(
    f"{os.environ.get('FINERACT_ADMIN_USERNAME', 'mifos')}:{os.environ.get('FINERACT_ADMIN_PASSWORD', '')}".encode()
).decode()
SELF_SIGNED = ssl._create_unverified_context()

RUN = f"{int(time.time()) % 1_000_000:06d}"
PREFIX = f"SIML-{RUN}"
CASES = ("MATCHED", "RENAMED", "NOT-SYNCED", "BRANCH", "ORPHAN", "CLASH")
CODE = {case: f"9{RUN}{i}" for i, case in enumerate(CASES)}
REF = {case: f"{PREFIX}-{case}" for case in CASES}
NAME = {case: f"Sim {case.title()} {RUN}" for case in CASES}

checks: list[tuple[str, bool]] = []


def call(method: str, url: str, body: dict[str, Any] | None = None, fineract: bool = False) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    for name, value in integration_headers(url).items():
        request.add_header(name, value)
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


def erp(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return call(method, f"{ERP_URL}{path}", body)


def fineract(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return call(method, f"{FINERACT_URL}{path}", body, fineract=True)


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def show(label: str, code: int, body: Any) -> None:
    print(f"  {label} -> {code} {json.dumps(body)}")


def erp_account(case: str) -> None:
    body = {"code": CODE[case], "name": NAME[case], "type": "ASSET", "currency": "RWF", "external_reference": REF[case]}
    code, response = erp("POST", "/api/accounts", body)
    show(f"ERP POST /api/accounts {REF[case]} code {CODE[case]}", code, response)


def stock_gl(case: str) -> None:
    body = {"glCode": CODE[case], "name": NAME[case], "type": 1, "usage": 1, "manualEntriesAllowed": True}
    code, response = fineract("POST", "/glaccounts", body)
    show(f"Fineract POST /glaccounts code {CODE[case]} (no external reference)", code, {"status": "created" if code == 200 else response})


def gl_sync(label: str) -> None:
    code, body = ours("POST", "/api/gl-accounts/sync", {"external_reference": f"{PREFIX}-SYNC-{label}"})
    mine = [a for a in (body or {}).get("accounts", []) if a["external_reference"].startswith(PREFIX)]
    show(f"POST /api/gl-accounts/sync {PREFIX}-SYNC-{label}", code, {"operation_status": (body or {}).get("operation_status"), "this_run": mine})


def counts() -> tuple[int, int]:
    _, gls = fineract("GET", "/glaccounts")
    _, accounts = erp("GET", "/api/accounts")
    return len(gls), len(accounts)


def report(label: str) -> dict[str, dict[str, Any]]:
    code, body = ours("GET", "/api/reconciliation/gl-accounts")
    print(f"  GET /api/reconciliation/gl-accounts ({label}) -> {code}")
    print(f"  summary (whole chart, earlier runs included): {json.dumps(body['summary'])}")
    rows: dict[str, dict[str, Any]] = {}
    for item in body["items"]:
        if item["gl_code"] in CODE.values():
            case = next(c for c, v in CODE.items() if v == item["gl_code"])
            rows[case] = item
            print(f"    {case:<11} {json.dumps(item)}")
    check(f"{label}: report answers 200", code == 200)
    return rows


def expect(rows: dict[str, dict[str, Any]], expected: dict[str, str], label: str) -> None:
    for case, status in expected.items():
        check(f"{label}: {case} is {status}", rows.get(case, {}).get("status") == status)


def main() -> int:
    print(f"== S-L1: seed the GL outcomes (run {RUN}) ==")
    erp_account("MATCHED")
    erp_account("RENAMED")
    gl_sync("1")
    _, renamed = fineract("GET", f"/glaccounts/external-reference/{REF['RENAMED']}")
    code, body = fineract("PUT", f"/glaccounts/{renamed['id']}", {"name": f"{NAME['RENAMED']} (edited)"})
    show(f"Fineract PUT /glaccounts/{{id}} for {REF['RENAMED']} name -> '{NAME['RENAMED']} (edited)'", code, {"changes": (body or {}).get("changes")})
    erp_account("NOT-SYNCED")
    stock_gl("BRANCH")
    orphan = {"externalReference": REF["ORPHAN"], "glCode": CODE["ORPHAN"], "name": NAME["ORPHAN"], "type": 1, "usage": 1, "manualEntriesAllowed": True}
    code, _ = fineract("POST", "/glaccounts/sync", orphan)
    show(f"Fineract POST /glaccounts/sync {REF['ORPHAN']} (no ERP account)", code, {"status": "created" if code == 200 else "failed"})
    stock_gl("CLASH")
    erp_account("CLASH")

    print("\n== S-L2: the report, read-only ==")
    before = counts()
    rows = report("before sync")
    expect(
        rows,
        {
            "MATCHED": "MATCHED",
            "RENAMED": "DETAILS_MISMATCH",
            "NOT-SYNCED": "MISSING_IN_FINERACT",
            "BRANCH": "MISSING_IN_ERP",
            "ORPHAN": "MISSING_IN_ERP",
            "CLASH": "CODE_CONFLICT",
        },
        "before sync",
    )
    check("RENAMED lists the name difference", any(d.startswith("name:") for d in rows.get("RENAMED", {}).get("differences", [])))
    check("CLASH has one row, carrying both sides", rows.get("CLASH", {}).get("erp") is not None and rows["CLASH"].get("fineract") is not None)
    check("NOT-SYNCED has no sync status yet", rows.get("NOT-SYNCED", {}).get("sync_status") is None)
    report_again = report("again")
    check("a second report gives the same rows", report_again == rows)
    after = counts()
    print(f"  Fineract GL accounts / ERP accounts before: {before}, after: {after}")
    check("the report wrote nothing to Fineract or the ERP", before == after)
    exposed = json.dumps(rows)
    check("no Fineract numeric id in the rows", f'"id": {renamed["id"]}' not in exposed and '"fineract_gl_account_id"' not in exposed)

    print("\n== S-L3: run the GL sync, then report again ==")
    gl_sync("2")
    rows = report("after sync")
    expect(
        rows,
        {
            "MATCHED": "MATCHED",
            "RENAMED": "DETAILS_MISMATCH",
            "NOT-SYNCED": "MATCHED",
            "BRANCH": "MISSING_IN_ERP",
            "ORPHAN": "MISSING_IN_ERP",
            "CLASH": "CODE_CONFLICT",
        },
        "after sync",
    )
    check("CLASH sync was refused, not adopted", rows.get("CLASH", {}).get("sync_status") in ("CONFLICT", "FAILED"))
    check("RENAMED was not overwritten by the create-only sync", rows.get("RENAMED", {}).get("fineract", {}).get("name") == f"{NAME['RENAMED']} (edited)")

    passed = sum(ok for _, ok in checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
