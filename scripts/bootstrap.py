"""Configures Fineract for the assessment through its REST API, as get-or-create steps.

Order: wait for health, office, currency, API role and user, ERP chart of accounts and GL sync, loan product.
Each step prints "created" or "exists, unchanged". A resource that exists with a different configuration
stops the script with a non-zero exit; nothing is ever updated silently. Running it twice changes nothing.
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
from typing import Any

ERP_URL = os.environ.get("ERP_URL", "http://localhost:8001")
INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")
FINERACT_ROOT = os.environ.get("FINERACT_ROOT", "https://localhost:8443/fineract-provider")
FINERACT_URL = f"{FINERACT_ROOT}/api/v1"
ADMIN_USERNAME = os.environ.get("FINERACT_ADMIN_USERNAME", "mifos")
ADMIN_PASSWORD = os.environ.get("FINERACT_ADMIN_PASSWORD", "password")
API_USERNAME = os.environ.get("FINERACT_API_USERNAME", "integration-api")
API_PASSWORD = os.environ.get("FINERACT_API_PASSWORD", "")
SELF_SIGNED = ssl._create_unverified_context()

DATE_FORMAT = "yyyy-MM-dd"
LOCALE = "en"

HEAD_OFFICE = "Head Office"
OFFICE = {"name": "Kigali Main Office", "externalId": "OFFICE-KGL-MAIN", "openingDate": "2026-01-01"}
CURRENCY = "RWF"

ROLE_NAME = "INTEGRATION_API"
ROLE_DESCRIPTION = "Integration service: GL sync, clients, loan lifecycle and reads only"
ROLE_PERMISSIONS = frozenset(
    {
        "READ_OFFICE",
        "READ_GLACCOUNT",
        "SYNC_GLACCOUNT",
        "READ_LOANPRODUCT",
        "CREATE_CLIENT",
        "READ_CLIENT",
        "CREATE_LOAN",
        "APPROVE_LOAN",
        "DISBURSE_LOAN",
        "REPAYMENT_LOAN",
        "READ_LOAN",
        "READ_JOURNALENTRY",
    }
)

CHART_OF_ACCOUNTS: list[dict[str, str]] = [
    {"code": "1100", "name": "Cash at Bank", "type": "ASSET"},
    {"code": "1200", "name": "Loan Receivable", "type": "ASSET"},
    {"code": "1300", "name": "Transfers in Suspense", "type": "ASSET"},
    {"code": "2100", "name": "Loan Overpayments", "type": "LIABILITY"},
    {"code": "4100", "name": "Interest Income on Loans", "type": "INCOME"},
    {"code": "4200", "name": "Fee Income", "type": "INCOME"},
    {"code": "4300", "name": "Penalty Income", "type": "INCOME"},
    {"code": "4400", "name": "Recoveries Income", "type": "INCOME"},
    {"code": "5100", "name": "Loan Losses Written Off", "type": "EXPENSE"},
]

PRODUCT_GL_MAPPINGS: dict[str, str] = {
    "fundSourceAccountId": "GL-1100",
    "loanPortfolioAccountId": "GL-1200",
    "transfersInSuspenseAccountId": "GL-1300",
    "overpaymentLiabilityAccountId": "GL-2100",
    "interestOnLoanAccountId": "GL-4100",
    "incomeFromFeeAccountId": "GL-4200",
    "incomeFromPenaltyAccountId": "GL-4300",
    "incomeFromRecoveryAccountId": "GL-4400",
    "writeOffAccountId": "GL-5100",
}

PRODUCT_TERMS: dict[str, Any] = {
    "name": "SME Asset Finance",
    "shortName": "SAF",
    "description": "SME asset finance: 18% a year, 12 monthly repayments, single disbursement",
    "currencyCode": CURRENCY,
    "digitsAfterDecimal": 0,
    "inMultiplesOf": 1,
    "minPrincipal": 100000,
    "principal": 1000000,
    "maxPrincipal": 50000000,
    "numberOfRepayments": 12,
    "repaymentEvery": 1,
    "repaymentFrequencyType": 2,
    "interestRatePerPeriod": 18,
    "interestRateFrequencyType": 3,
    "interestType": 0,
    "amortizationType": 1,
    "interestCalculationPeriodType": 1,
    "daysInYearType": 1,
    "daysInMonthType": 1,
    "isInterestRecalculationEnabled": False,
    "repaymentStartDateType": 1,
    "transactionProcessingStrategyCode": "mifos-standard-strategy",
    "multiDisburseLoan": False,
    "includeInBorrowerCycle": False,
    "accountingRule": 2,
}


class BootstrapError(Exception):
    pass


def _auth(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def call(method: str, url: str, body: dict[str, Any] | None = None, auth: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    is_fineract = url.startswith(FINERACT_ROOT)
    if is_fineract:
        request.add_header("Authorization", auth or _auth(ADMIN_USERNAME, ADMIN_PASSWORD))
        request.add_header("Fineract-Platform-TenantId", "default")
    try:
        with urllib.request.urlopen(request, context=SELF_SIGNED if is_fineract else None, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except ValueError:
            return exc.code, raw.decode(errors="replace")


def fineract(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    status, payload = call(method, f"{FINERACT_URL}{path}", body)
    if status >= 400:
        raise BootstrapError(f"Fineract {method} {path} -> {status}: {json.dumps(payload)}")
    return payload


def report(step: str, outcome: str) -> None:
    print(f"  {step:<50} {outcome}")


def mismatch(step: str, differences: dict[str, tuple[Any, Any]]) -> BootstrapError:
    lines = "\n".join(f"    {key}: expected {want!r}, found {have!r}" for key, (want, have) in differences.items())
    return BootstrapError(f"{step} exists with a different configuration; not changing it:\n{lines}")


def diff(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    return {key: (want, actual.get(key)) for key, want in expected.items() if actual.get(key) != want}


def wait_for_health(timeout_seconds: int = 900) -> None:
    targets = {
        "Fineract": f"{FINERACT_ROOT}/actuator/health",
        "Mock ERP": f"{ERP_URL}/health",
        "Integration Service": f"{INTEGRATION_URL}/health",
    }
    deadline = time.monotonic() + timeout_seconds
    for name, url in targets.items():
        while True:
            try:
                status, _ = call("GET", url)
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                status = 0
            if status == 200:
                report(f"{name} health", "up")
                break
            if time.monotonic() > deadline:
                raise BootstrapError(f"{name} not healthy at {url} after {timeout_seconds}s")
            time.sleep(5)


def ensure_office() -> int:
    offices: list[dict[str, Any]] = fineract("GET", "/offices")
    parent = next((o for o in offices if o["name"] == HEAD_OFFICE), None)
    if parent is None:
        raise BootstrapError(f"stock office {HEAD_OFFICE!r} not found")
    existing = next((o for o in offices if o["name"] == OFFICE["name"]), None)
    step = f"office {OFFICE['name']!r}"
    if existing is None:
        created = fineract(
            "POST",
            "/offices",
            {**OFFICE, "parentId": parent["id"], "dateFormat": DATE_FORMAT, "locale": LOCALE},
        )
        report(step, "created")
        return int(created["officeId"])
    opening = existing.get("openingDate")
    actual = {
        "externalId": existing.get("externalId"),
        "parentId": existing.get("parentId"),
        "openingDate": "-".join(f"{part:02d}" for part in opening) if isinstance(opening, list) else opening,
    }
    differences = diff({"externalId": OFFICE["externalId"], "parentId": parent["id"], "openingDate": OFFICE["openingDate"]}, actual)
    if differences:
        raise mismatch(step, differences)
    report(step, "exists, unchanged")
    return int(existing["id"])


def ensure_currency() -> None:
    config = fineract("GET", "/currencies")
    selected = [c["code"] for c in config["selectedCurrencyOptions"]]
    step = f"currency {CURRENCY}"
    if CURRENCY in selected:
        report(step, "exists, unchanged")
        return
    fineract("PUT", "/currencies", {"currencies": [*selected, CURRENCY]})
    report(step, f"added (kept {', '.join(selected) or 'none'})")


def ensure_role() -> int:
    roles: list[dict[str, Any]] = fineract("GET", "/roles")
    existing = next((r for r in roles if r["name"] == ROLE_NAME), None)
    step = f"role {ROLE_NAME}"
    if existing is None:
        role_id = int(fineract("POST", "/roles", {"name": ROLE_NAME, "description": ROLE_DESCRIPTION})["resourceId"])
        fineract("PUT", f"/roles/{role_id}/permissions", {"permissions": {code: True for code in ROLE_PERMISSIONS}})
        report(step, f"created with {len(ROLE_PERMISSIONS)} permissions")
        return role_id
    role_id = int(existing["id"])
    usage = fineract("GET", f"/roles/{role_id}/permissions")["permissionUsageData"]
    granted = {p["code"] for p in usage if p.get("selected")}
    differences = diff(
        {"permissions": sorted(ROLE_PERMISSIONS), "disabled": False},
        {"permissions": sorted(granted), "disabled": existing.get("disabled")},
    )
    if differences:
        raise mismatch(step, differences)
    report(step, "exists, unchanged")
    return role_id


def ensure_api_user(office_id: int, role_id: int) -> None:
    users: list[dict[str, Any]] = fineract("GET", "/users")
    existing = next((u for u in users if u["username"] == API_USERNAME), None)
    step = f"user {API_USERNAME}"
    if existing is None:
        if not API_PASSWORD:
            raise BootstrapError("FINERACT_API_PASSWORD is not set; it is needed to create the API user")
        fineract(
            "POST",
            "/users",
            {
                "username": API_USERNAME,
                "firstname": "Integration",
                "lastname": "Service",
                "email": f"{API_USERNAME}@integration.invalid",
                "officeId": office_id,
                "roles": [role_id],
                "sendPasswordToEmail": False,
                "password": API_PASSWORD,
                "repeatPassword": API_PASSWORD,
                "passwordNeverExpires": True,
            },
        )
        report(step, "created")
    else:
        actual = {"officeId": existing.get("officeId"), "roles": sorted(r["id"] for r in existing.get("selectedRoles", []))}
        differences = diff({"officeId": office_id, "roles": [role_id]}, actual)
        if differences:
            raise mismatch(step, differences)
        report(step, "exists, unchanged")
    status, payload = call("GET", f"{FINERACT_URL}/offices", auth=_auth(API_USERNAME, API_PASSWORD))
    if status != 200:
        raise BootstrapError(f"{API_USERNAME} cannot call Fineract (GET /offices -> {status}: {payload})")
    report(f"user {API_USERNAME} login", "ok")


def ensure_gl_accounts() -> dict[str, int]:
    for account in CHART_OF_ACCOUNTS:
        reference = f"GL-{account['code']}"
        status, payload = call("POST", f"{ERP_URL}/api/accounts", {**account, "currency": CURRENCY, "external_reference": reference})
        if status not in (200, 201):
            raise BootstrapError(f"ERP account {reference} -> {status}: {json.dumps(payload)}")
        report(f"ERP account {reference} {account['name']!r}", "created" if status == 201 else "exists, unchanged")

    status, payload = call("POST", f"{INTEGRATION_URL}/api/gl-accounts/sync")
    if status != 200:
        raise BootstrapError(f"GL sync -> {status}: {json.dumps(payload)}")
    status, rows = call("GET", f"{INTEGRATION_URL}/api/gl-accounts")
    by_reference = {row["external_reference"]: row for row in rows}

    ids: dict[str, int] = {}
    for reference in PRODUCT_GL_MAPPINGS.values():
        row = by_reference.get(reference)
        if row is None or row["status"] != "SYNCED":
            raise BootstrapError(f"GL account {reference} is not synced: {json.dumps(row)}")
        ids[reference] = int(fineract("GET", f"/glaccounts/external-reference/{reference}")["id"])
    report(f"GL sync ({len(ids)} accounts)", "all SYNCED")
    return ids


def _id_of(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


def ensure_loan_product(gl_ids: dict[str, int]) -> None:
    mappings = {field: gl_ids[reference] for field, reference in PRODUCT_GL_MAPPINGS.items()}
    products: list[dict[str, Any]] = fineract("GET", "/loanproducts")
    existing = next((p for p in products if p["shortName"] == PRODUCT_TERMS["shortName"]), None)
    step = f"loan product {PRODUCT_TERMS['shortName']} {PRODUCT_TERMS['name']!r}"
    if existing is None:
        fineract("POST", "/loanproducts", {**PRODUCT_TERMS, **mappings, "locale": LOCALE, "dateFormat": DATE_FORMAT})
        report(step, "created")
        return

    product = fineract("GET", f"/loanproducts/{existing['id']}")
    actual = {
        "name": product.get("name"),
        "currencyCode": product.get("currency", {}).get("code"),
        "digitsAfterDecimal": product.get("currency", {}).get("decimalPlaces"),
        "minPrincipal": product.get("minPrincipal"),
        "principal": product.get("principal"),
        "maxPrincipal": product.get("maxPrincipal"),
        "numberOfRepayments": product.get("numberOfRepayments"),
        "repaymentEvery": product.get("repaymentEvery"),
        "repaymentFrequencyType": _id_of(product.get("repaymentFrequencyType")),
        "interestRatePerPeriod": product.get("interestRatePerPeriod"),
        "interestRateFrequencyType": _id_of(product.get("interestRateFrequencyType")),
        "interestType": _id_of(product.get("interestType")),
        "amortizationType": _id_of(product.get("amortizationType")),
        "interestCalculationPeriodType": _id_of(product.get("interestCalculationPeriodType")),
        "transactionProcessingStrategyCode": product.get("transactionProcessingStrategyCode"),
        "multiDisburseLoan": product.get("multiDisburseLoan"),
        "accountingRule": _id_of(product.get("accountingRule")),
    }
    expected = {key: PRODUCT_TERMS[key] for key in actual}
    account_mappings = product.get("accountingMappings", {})
    for field, gl_id in mappings.items():
        mapping_key = field.removesuffix("Id")
        expected[field] = gl_id
        actual[field] = _id_of(account_mappings.get(mapping_key))
    differences = diff(expected, actual)
    if differences:
        raise mismatch(step, differences)
    report(step, "exists, unchanged")


def main() -> int:
    if API_USERNAME == ADMIN_USERNAME:
        print(f"BOOTSTRAP FAILED: the API user must not be the admin user ({ADMIN_USERNAME})", file=sys.stderr)
        return 1
    try:
        print("Health")
        wait_for_health()
        print("Organisation")
        office_id = ensure_office()
        ensure_currency()
        print("API user")
        role_id = ensure_role()
        ensure_api_user(office_id, role_id)
        print("Chart of accounts")
        gl_ids = ensure_gl_accounts()
        print("Loan product")
        ensure_loan_product(gl_ids)
    except BootstrapError as exc:
        print(f"\nBOOTSTRAP FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nBootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
