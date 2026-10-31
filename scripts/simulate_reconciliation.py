"""Part K: seed all four reconciliation outcomes plus a reversal, show the report, then repair what may be repaired.

Seeded on one new loan, inside a three-day window used by no other run:
  DISBURSE, REPAY-1     through the integration API                    -> MATCHED
  REPAY-UNDONE          through the API, then reversed in Fineract      -> MATCHED (original) + MISSING_IN_ERP (-REV)
  BRANCH-1              repayment booked straight in Fineract           -> MISSING_IN_ERP
  BRANCH-2              straight in Fineract, ERP entry with a typo     -> AMOUNT_MISMATCH
  ERP-ONLY              journal entry posted straight to the ERP        -> MISSING_IN_FINERACT

The direct Fineract calls use the admin user and stand in for a back-office user working outside the integration.
The direct ERP post stands in for a manual journal. Uses only the standard library.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from integration_auth import integration_headers

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = base64.b64encode(
    f"{os.environ.get('FINERACT_ADMIN_USERNAME', 'mifos')}:{os.environ.get('FINERACT_ADMIN_PASSWORD', '')}".encode()
).decode()
ERP_DB_PASSWORD = os.environ.get("ERP_DB_PASSWORD", "")
SELF_SIGNED = ssl._create_unverified_context()

RUN = f"{int(time.time()) % 1_000_000:06d}"
PREFIX = f"SIMK-{RUN}"
CLIENT_UID = f"SME-{(int(RUN) + 500_000) % 1_000_000:06d}"
CLIENT_NAME = f"Reconcile Traders {RUN} Ltd"
PRINCIPAL = 5_000_000
DAY_0 = date(2026, 6, 1) + timedelta(days=(int(RUN) % 40) * 3)
DAY_1 = DAY_0 + timedelta(days=1)
DAY_2 = DAY_0 + timedelta(days=2)

REF = {
    "disburse": f"{PREFIX}-DISBURSE",
    "repay": f"{PREFIX}-REPAY-1",
    "undone": f"{PREFIX}-REPAY-UNDONE",
    "branch_missing": f"{PREFIX}-BRANCH-1",
    "branch_typo": f"{PREFIX}-BRANCH-2",
    "erp_only": f"{PREFIX}-ERP-ONLY",
    "unknown": f"{PREFIX}-NEVER-HAPPENED",
}
REV = f"{REF['undone']}-REV"

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


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def show(label: str, code: int, body: Any) -> None:
    print(f"  {label} -> {code} {json.dumps(body)}")


def erp_rows(where: str = "TRUE") -> int:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", f"PGPASSWORD={ERP_DB_PASSWORD}", "db",
         "psql", "-U", "erp", "-d", "erp", "-tAc", f"SELECT count(*) FROM journal_entries WHERE {where}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    return int(out.strip())


def fineract_transactions(loan_reference: str) -> list[tuple[str | None, str, int, bool]]:
    _, loan = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}?associations=transactions", fineract=True)
    return [
        (t.get("externalId"), t["type"]["value"], int(t["amount"]), bool(t.get("manuallyReversed")))
        for t in loan.get("transactions") or []
    ]


def fineract_repayment(loan_reference: str, reference: str, amount: int, on: date) -> dict[str, Any]:
    payload = {
        "transactionDate": on.isoformat(),
        "transactionAmount": amount,
        "externalId": reference,
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }
    code, body = call("POST", f"{FINERACT_URL}/loans/external-id/{loan_reference}/transactions?command=repayment", payload, fineract=True)
    check(f"Fineract booked {reference} directly ({code})", code == 200)
    _, transaction = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}/transactions/external-id/{reference}", fineract=True)
    return transaction


def setup() -> str:
    print(f"Setup: client, loan, approval and disbursement through the integration API, window {DAY_0} to {DAY_2}")
    code, _ = ours("POST", "/api/clients", {"external_reference": f"{PREFIX}-CLIENT", "client_uid": CLIENT_UID, "name": CLIENT_NAME, "activation_date": DAY_0.isoformat()})
    check("client created (201)", code == 201)
    if code != 201:
        raise SystemExit(f"setup failed: {code}")
    code, body = ours("POST", "/api/loans", {"external_reference": f"{PREFIX}-APPLY", "client_uid": CLIENT_UID, "client_name": CLIENT_NAME, "principal": PRINCIPAL, "submitted_on_date": DAY_0.isoformat()})
    check("loan application created (201)", code == 201)
    if code != 201:
        raise SystemExit(f"setup failed: {code} {body}")
    loan_reference: str = body["loan_reference"]
    code, _ = ours("POST", f"/api/loans/{loan_reference}/approval", {"external_reference": f"{PREFIX}-APPROVE", "approved_on_date": DAY_0.isoformat()})
    check("loan approved (201)", code == 201)
    code, _ = ours("POST", f"/api/loans/{loan_reference}/disbursement", {"external_reference": REF["disburse"], "disbursed_on_date": DAY_0.isoformat()})
    check("disbursed through the API, ERP entry posted (201)", code == 201)
    print(f"  loan {loan_reference}, principal {PRINCIPAL} RWF")
    return loan_reference


def seed(loan_reference: str) -> None:
    print("\nSeed MATCHED: repayment through the integration API")
    code, body = ours("POST", f"/api/loans/{loan_reference}/repayments", {"external_reference": REF["repay"], "amount": 500_000, "transaction_date": DAY_1.isoformat()})
    show("POST repayment", code, body)
    check("repayment completed with an ERP entry (201)", code == 201 and body.get("erp_transaction_id") is not None)

    print("\nSeed a reversal: repayment through the API, then reversed in Fineract (no undo route exists in our API)")
    code, body = ours("POST", f"/api/loans/{loan_reference}/repayments", {"external_reference": REF["undone"], "amount": 200_000, "transaction_date": DAY_1.isoformat()})
    check("repayment completed with an ERP entry (201)", code == 201)
    undo = {"transactionDate": DAY_1.isoformat(), "transactionAmount": 0, "dateFormat": "yyyy-MM-dd", "locale": "en"}
    code, body = call("POST", f"{FINERACT_URL}/loans/external-id/{loan_reference}/transactions/external-id/{REF['undone']}", undo, fineract=True)
    show("Fineract undo (adjust to 0)", code, body)
    _, transaction = call("GET", f"{FINERACT_URL}/loans/external-id/{loan_reference}/transactions/external-id/{REF['undone']}", fineract=True)
    check("Fineract marks it manuallyReversed", code == 200 and transaction.get("manuallyReversed") is True)
    print(f"  reversedOnDate {transaction.get('reversedOnDate')}")

    print("\nSeed MISSING_IN_ERP: repayment booked straight in Fineract")
    fineract_repayment(loan_reference, REF["branch_missing"], 300_000, DAY_2)

    print("\nSeed AMOUNT_MISMATCH: repayment straight in Fineract, ERP entry keyed in with a typo")
    t = fineract_repayment(loan_reference, REF["branch_typo"], 250_000, DAY_2)
    principal, interest = int(t["principalPortion"]), int(t.get("interestPortion") or 0)
    typo = principal - 25_000
    lines = [{"account_code": "1100", "debit": typo + interest, "credit": 0}, {"account_code": "1200", "debit": 0, "credit": typo}]
    if interest:
        lines.append({"account_code": "4100", "debit": 0, "credit": interest})
    code, body = call("POST", f"{ERP_URL}/api/journal-entries", {"external_reference": REF["branch_typo"], "entry_date": DAY_2.isoformat(), "description": f"Manual: loan {loan_reference} repayment", "currency": "RWF", "lines": lines})
    print(f"  Fineract split: principal {principal}, interest {interest}; ERP keyed principal {typo}")
    check(f"ERP accepted the mistyped entry ({code})", code == 201)

    print("\nSeed MISSING_IN_FINERACT: manual ERP entry with no Fineract transaction")
    code, _ = call("POST", f"{ERP_URL}/api/journal-entries", {"external_reference": REF["erp_only"], "entry_date": DAY_2.isoformat(), "description": f"Manual: loan {loan_reference} repayment", "currency": "RWF", "lines": [{"account_code": "1100", "debit": 150_000, "credit": 0}, {"account_code": "1200", "debit": 0, "credit": 150_000}]})
    check(f"ERP accepted the entry ({code})", code == 201)


def report(label: str, request: dict[str, Any]) -> dict[str, Any]:
    code, body = ours("POST", "/api/reconciliation", request)
    print(f"\n{label}: POST /api/reconciliation {json.dumps(request)} -> {code}")
    if code != 200:
        print(f"  {json.dumps(body)}")
        check(f"{label}: 200", False)
        return {"items": [], "summary": {}, "not_found": []}
    print(f"  summary {json.dumps(body['summary'])}, loans scanned {body['loans_scanned']}, not_found {body['not_found']}")
    for item in body["items"]:
        fineract = item["fineract"]
        erp = item["erp"]
        print(
            f"  {item['status']:<20} {item['external_reference']:<32} op={item['operation_status'] or '-':<10} "
            f"fineract={fineract['type'] + ' ' + str(fineract['amount']) + (' reversed' if fineract['reversed'] else '') if fineract else '-':<26} "
            f"erp={erp['erp_transaction_id'] + ' ' + str(erp['amount']) if erp else '-'}"
        )
        for difference in item["differences"]:
            print(f"  {'':<20}   difference: {difference}")
        if item["note"]:
            print(f"  {'':<20}   note: {item['note']}")
    return dict(body)


def statuses(body: dict[str, Any]) -> dict[str, str]:
    return {item["external_reference"]: item["status"] for item in body["items"]}


EXPECTED = {
    REF["disburse"]: "MATCHED",
    REF["repay"]: "MATCHED",
    REF["undone"]: "MATCHED",
    REV: "MISSING_IN_ERP",
    REF["branch_missing"]: "MISSING_IN_ERP",
    REF["branch_typo"]: "AMOUNT_MISMATCH",
    REF["erp_only"]: "MISSING_IN_FINERACT",
}


def reconcile_and_check(loan_reference: str) -> None:
    rows_before = erp_rows()
    fineract_before = fineract_transactions(loan_reference)

    by_reference = report("Report by references", {"external_references": [REF[k] for k in ("disburse", "repay", "undone", "branch_missing", "branch_typo", "erp_only", "unknown")]})
    check("references: every seeded reference has its expected status", statuses(by_reference) == EXPECTED)
    check("references: a reference neither system knows is listed in not_found", by_reference["not_found"] == [REF["unknown"]])
    check("references: the summary shows all four outcomes", all(by_reference["summary"].get(s, 0) > 0 for s in ("MATCHED", "MISSING_IN_ERP", "MISSING_IN_FINERACT", "AMOUNT_MISMATCH")))
    mismatch = next(i for i in by_reference["items"] if i["external_reference"] == REF["branch_typo"])
    check("references: the mismatch names the amount and the 1200 credit", any(d.startswith("amount:") for d in mismatch["differences"]) and any(d.startswith("1200 credit:") for d in mismatch["differences"]))
    check("no Fineract numeric id appears in the report", not any("fineract_id" in json.dumps(i) or '"id"' in json.dumps(i) for i in by_reference["items"]))

    by_range = report("Report by date range", {"from_date": DAY_0.isoformat(), "to_date": DAY_2.isoformat()})
    check("date range: same statuses as by references", statuses(by_range) == EXPECTED)

    report("Report by references, run again", {"external_references": [REF["branch_missing"]]})
    check(f"read-only: ERP row count unchanged ({rows_before} -> {erp_rows()})", erp_rows() == rows_before)
    check("read-only: Fineract transactions unchanged", fineract_transactions(loan_reference) == fineract_before)


def repair(loan_reference: str) -> None:
    path = "/api/reconciliation/repairs"

    print("\nRepair MISSING_IN_ERP (BRANCH-1), same request twice, then with a second reference")
    request = {"external_reference": f"{PREFIX}-FIX-1", "loan_reference": loan_reference, "transaction_reference": REF["branch_missing"]}
    first = ours("POST", path, request)
    show("repair", *first)
    replay = ours("POST", path, request)
    show("repair again", *replay)
    other = ours("POST", path, {**request, "external_reference": f"{PREFIX}-FIX-1B"})
    show("repair with another reference", *other)
    check("first repair 201 with an ERP entry", first[0] == 201 and first[1].get("erp_transaction_id") is not None)
    check("replay 200, same erp_transaction_id", replay[0] == 200 and replay[1].get("erp_transaction_id") == first[1].get("erp_transaction_id"))
    check("second reference finds the same entry", other[0] == 201 and other[1].get("erp_transaction_id") == first[1].get("erp_transaction_id"))
    check(f"ERP holds exactly one {REF['branch_missing']}", erp_rows(f"external_reference = '{REF['branch_missing']}'") == 1)
    conflict = ours("POST", path, {**request, "transaction_reference": REF["branch_typo"]})
    show("same reference, different payload", *conflict)
    check("same repair reference with a different payload: 409", conflict[0] == 409)

    print("\nRepair the missing reversal (REPAY-UNDONE-REV)")
    code, body = ours("POST", path, {"external_reference": f"{PREFIX}-FIX-2", "loan_reference": loan_reference, "transaction_reference": REV})
    show("repair", code, body)
    check("reversal posted (201)", code == 201)

    print("\nRepairs that must be refused")
    code, body = ours("POST", path, {"external_reference": f"{PREFIX}-FIX-3", "loan_reference": loan_reference, "transaction_reference": REF["branch_typo"]})
    show("AMOUNT_MISMATCH", code, body)
    check("AMOUNT_MISMATCH is not repaired: 409, ERP entry untouched", code == 409 and erp_rows(f"external_reference = '{REF['branch_typo']}'") == 1)
    code, body = ours("POST", path, {"external_reference": f"{PREFIX}-FIX-4", "loan_reference": loan_reference, "transaction_reference": REF["erp_only"]})
    show("MISSING_IN_FINERACT", code, body)
    check("MISSING_IN_FINERACT is not repaired: 404, nothing written to Fineract", code == 404)
    code, body = ours("POST", path, {"external_reference": f"{PREFIX}-FIX-5", "loan_reference": loan_reference, "transaction_reference": f"{REF['repay']}-REV"})
    show("reversal of a live transaction", code, body)
    check("a reversal Fineract does not have is refused: 409", code == 409)

    after = report("Report by date range after the repairs", {"from_date": DAY_0.isoformat(), "to_date": DAY_2.isoformat()})
    check(
        "after repair: BRANCH-1 and the reversal MATCHED; the mismatch and the ERP-only entry still need a person",
        statuses(after) == {**EXPECTED, REV: "MATCHED", REF["branch_missing"]: "MATCHED"},
    )


def main() -> int:
    print(f"Run {RUN}: Part K reconciliation\n")
    loan_reference = setup()
    seed(loan_reference)
    reconcile_and_check(loan_reference)
    repair(loan_reference)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
