"""Partner client to Fineract client mapping and validation. Pure functions, no I/O."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

CLIENT_UID_PATTERN = r"^SME-\d{6}$"
FINERACT_FULLNAME_MAX_LENGTH = 160
FINERACT_LEGAL_FORM_ENTITY = 2
FINERACT_DATE_FORMAT = "yyyy-MM-dd"
FINERACT_LOCALE = "en"
FINERACT_DUPLICATE_EXTERNAL_ID = "error.msg.client.duplicate.externalId"


@dataclass(frozen=True)
class CreateClient:
    external_reference: str
    client_uid: str
    name: str
    activation_date: date | None

    def payload(self) -> dict[str, Any]:
        return {
            "client_uid": self.client_uid,
            "name": self.name,
            "activation_date": self.activation_date.isoformat() if self.activation_date else None,
        }


@dataclass(frozen=True)
class FineractClientPayload:
    officeId: int
    legalFormId: int
    fullname: str
    externalId: str
    active: bool
    activationDate: str
    dateFormat: str
    locale: str


@dataclass(frozen=True)
class FineractClientRecord:
    fineract_id: int
    external_id: str | None
    name: str
    active: bool
    status: str
    office_name: str
    activation_date: date | None


@dataclass(frozen=True)
class ClientValidation:
    client_uid: str
    exists: bool
    active: bool
    name_matches: bool

    @property
    def valid(self) -> bool:
        return self.exists and self.active and self.name_matches


def is_valid_client_uid(client_uid: str) -> bool:
    return re.fullmatch(CLIENT_UID_PATTERN, client_uid) is not None


def normalise_name(name: str) -> str:
    return " ".join(name.split())


def names_match(expected: str, actual: str) -> bool:
    return normalise_name(expected).casefold() == normalise_name(actual).casefold()


def to_fineract_client(client_uid: str, name: str, activation_date: date, office_id: int) -> FineractClientPayload:
    return FineractClientPayload(
        officeId=office_id,
        legalFormId=FINERACT_LEGAL_FORM_ENTITY,
        fullname=name,
        externalId=client_uid,
        active=True,
        activationDate=activation_date.isoformat(),
        dateFormat=FINERACT_DATE_FORMAT,
        locale=FINERACT_LOCALE,
    )


def parse_fineract_date(value: Any) -> date | None:
    if isinstance(value, list) and len(value) == 3:
        return date(int(value[0]), int(value[1]), int(value[2]))
    if isinstance(value, str):
        return date.fromisoformat(value)
    return None


def parse_fineract_client(body: dict[str, Any]) -> FineractClientRecord:
    status = body.get("status") or {}
    return FineractClientRecord(
        fineract_id=int(body["id"]),
        external_id=body.get("externalId"),
        name=str(body.get("displayName") or body.get("fullname") or ""),
        active=bool(body.get("active")),
        status=str(status.get("value") or status.get("code") or "unknown"),
        office_name=str(body.get("officeName") or ""),
        activation_date=parse_fineract_date(body.get("activationDate")),
    )


def validate_client(client_uid: str, record: FineractClientRecord | None, expected_name: str) -> ClientValidation:
    if record is None:
        return ClientValidation(client_uid=client_uid, exists=False, active=False, name_matches=False)
    return ClientValidation(
        client_uid=client_uid,
        exists=True,
        active=record.active,
        name_matches=names_match(expected_name, record.name),
    )
