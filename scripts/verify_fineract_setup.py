"""Reads the bootstrapped Fineract configuration back as the integration API user and checks its privileges.

Expected: office, RWF and the SME Asset Finance product (terms and GL mappings, by GL external reference) are
as bootstrap configured them; the API user can read them but is refused (403) on admin actions.
Known upstream gap, reported separately: Fineract 1.16.1 does not authorize PUT /currencies (DECISIONS.md D-013).
The probe sends an unknown currency code, so the handler rejects it and the currency list never changes.
Run after `make bootstrap`. Uses only the standard library so it runs on the host without a virtualenv.
"""

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

from bootstrap import CURRENCY, OFFICE, PRODUCT_GL_MAPPINGS, PRODUCT_TERMS

FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
API_USERNAME = os.environ.get("FINERACT_API_USERNAME", "integration-api")
API_PASSWORD = os.environ.get("FINERACT_API_PASSWORD", "")
SELF_SIGNED = ssl._create_unverified_context()

FORBIDDEN_ACTIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/offices", {"name": "Not Allowed", "parentId": 1, "openingDate": "2026-01-01", "dateFormat": "yyyy-MM-dd", "locale": "en"}),
    ("POST", "/loanproducts", {"name": "Not Allowed", "shortName": "NOPE"}),
    ("POST", "/users", {"username": "not-allowed"}),
    ("POST", "/roles", {"name": "NOT_ALLOWED", "description": "x"}),
    ("POST", "/glaccounts", {"name": "Not Allowed", "glCode": "9999", "type": 1, "usage": 1, "manualEntriesAllowed": True}),
    ("POST", "/journalentries", {"officeId": 1}),
]


def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"{FINERACT_URL}{path}", method=method, data=json.dumps(body).encode() if body is not None else None
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{API_USERNAME}:{API_PASSWORD}".encode()).decode())
    request.add_header("Fineract-Platform-TenantId", "default")
    try:
        with urllib.request.urlopen(request, context=SELF_SIGNED, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except ValueError:
            return exc.code, raw.decode(errors="replace")


def check(results: list[bool], label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f'  ({detail})' if detail else ''}")


def main() -> int:
    results: list[bool] = []
    print(f"As {API_USERNAME}: configuration read-back")

    status, offices = call("GET", "/offices")
    office = next((o for o in offices if o["name"] == OFFICE["name"]), None) if status == 200 else None
    check(results, f"office {OFFICE['name']!r}", office is not None and office.get("externalId") == OFFICE["externalId"],
          f"externalId={office.get('externalId') if office else None}")

    status, products = call("GET", "/loanproducts")
    summary = next((p for p in products if p["shortName"] == PRODUCT_TERMS["shortName"]), None) if status == 200 else None
    check(results, f"loan product {PRODUCT_TERMS['shortName']} readable", summary is not None, f"GET /loanproducts -> {status}")
    if summary is not None:
        _, product = call("GET", f"/loanproducts/{summary['id']}")
        terms = {
            "currency": product["currency"]["code"],
            "decimals": product["currency"]["decimalPlaces"],
            "interest": f"{product['interestRatePerPeriod']:g}% {product['interestRateFrequencyType']['value']}",
            "repayments": f"{product['numberOfRepayments']} x every {product['repaymentEvery']} {product['repaymentFrequencyType']['value']}",
            "interest method": product["interestType"]["value"],
            "amortization": product["amortizationType"]["value"],
            "multi-disburse": product["multiDisburseLoan"],
            "accounting": product["accountingRule"]["value"],
        }
        for key, value in terms.items():
            print(f"        {key:<16} {value}")
        check(
            results,
            "product terms: RWF, 18%/year, 12 monthly, single disbursement, cash",
            terms["currency"] == CURRENCY
            and product["interestRatePerPeriod"] == 18
            and product["interestRateFrequencyType"]["id"] == 3
            and product["numberOfRepayments"] == 12
            and product["repaymentFrequencyType"]["id"] == 2
            and product["multiDisburseLoan"] is False
            and product["accountingRule"]["id"] == 2,
        )
        mappings = product.get("accountingMappings", {})
        for field, reference in PRODUCT_GL_MAPPINGS.items():
            mapped = mappings.get(field.removesuffix("Id")) or {}
            _, account = call("GET", f"/glaccounts/{mapped.get('id')}") if mapped else (0, {})
            actual = account.get("externalReference") if isinstance(account, dict) else None
            check(results, f"{field.removesuffix('AccountId'):<22} -> {reference}", actual == reference,
                  f"{mapped.get('glCode')} {mapped.get('name')}")

    print(f"\nAs {API_USERNAME}: admin actions must be refused (403)")
    for method, path, body in FORBIDDEN_ACTIONS:
        status, payload = call(method, path, body)
        code = payload.get("userMessageGlobalisationCode") if isinstance(payload, dict) else None
        check(results, f"{method} {path}", status == 403, f"{status} {code}")

    print(f"\nAs {API_USERNAME}: known upstream gap (not counted)")
    status, payload = call("PUT", "/currencies", {"currencies": ["ZZZ"]})
    code = payload.get("userMessageGlobalisationCode") if isinstance(payload, dict) else None
    reached = "reached the handler: not authorized by Fineract" if status != 403 else "refused"
    print(f"  GAP   PUT /currencies  ({status} {code}; {reached})")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed: {'PASS' if all(results) else 'FAIL'}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
