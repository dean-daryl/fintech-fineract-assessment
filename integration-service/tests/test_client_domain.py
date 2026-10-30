from dataclasses import asdict
from datetime import date

import pytest

from app.domain.clients import (
    CreateClient,
    FineractClientRecord,
    is_valid_client_uid,
    names_match,
    parse_fineract_client,
    to_fineract_client,
    validate_client,
)


@pytest.mark.parametrize("client_uid", ["SME-000001", "SME-999999"])
def test_accepts_client_uid(client_uid: str) -> None:
    assert is_valid_client_uid(client_uid)


@pytest.mark.parametrize("client_uid", ["SME-1", "sme-000001", "SME-0000001", "SME-00000A", "LN-2026-000001", "", "SME-000001\n"])
def test_rejects_client_uid(client_uid: str) -> None:
    assert not is_valid_client_uid(client_uid)


def test_names_match_ignores_case_and_spacing() -> None:
    assert names_match("acme  ltd", " Acme Ltd ")
    assert not names_match("Acme Ltd", "Acme Limited")


def test_payload_hashes_the_partner_fields_only() -> None:
    request = CreateClient(external_reference="REF-1", client_uid="SME-000001", name="Acme Ltd", activation_date=date(2026, 3, 1))
    assert request.payload() == {"client_uid": "SME-000001", "name": "Acme Ltd", "activation_date": "2026-03-01"}


def test_fineract_payload_is_an_active_entity_keyed_by_client_uid() -> None:
    payload = asdict(to_fineract_client("SME-000001", "Acme Ltd", date(2026, 3, 1), office_id=2))
    assert payload == {
        "officeId": 2,
        "legalFormId": 2,
        "fullname": "Acme Ltd",
        "externalId": "SME-000001",
        "active": True,
        "activationDate": "2026-03-01",
        "dateFormat": "yyyy-MM-dd",
        "locale": "en",
    }


def test_parses_fineract_client() -> None:
    record = parse_fineract_client(
        {
            "id": 7,
            "externalId": "SME-000001",
            "displayName": "Acme Ltd",
            "active": True,
            "status": {"id": 300, "code": "clientStatusType.active", "value": "Active"},
            "officeName": "Kigali Main Office",
            "activationDate": [2026, 3, 1],
        }
    )
    assert record == FineractClientRecord(
        fineract_id=7,
        external_id="SME-000001",
        name="Acme Ltd",
        active=True,
        status="Active",
        office_name="Kigali Main Office",
        activation_date=date(2026, 3, 1),
    )


def _record(active: bool = True, name: str = "Acme Ltd") -> FineractClientRecord:
    return FineractClientRecord(7, "SME-000001", name, active, "Active" if active else "Pending", "Kigali Main Office", None)


def test_validation_passes_for_active_client_with_matching_name() -> None:
    assert validate_client("SME-000001", _record(), "ACME LTD").valid


def test_validation_reports_each_failure() -> None:
    missing = validate_client("SME-000001", None, "Acme Ltd")
    assert (missing.exists, missing.valid) == (False, False)
    inactive = validate_client("SME-000001", _record(active=False), "Acme Ltd")
    assert (inactive.exists, inactive.active, inactive.valid) == (True, False, False)
    renamed = validate_client("SME-000001", _record(name="Beta Corp"), "Acme Ltd")
    assert (renamed.name_matches, renamed.valid) == (False, False)
