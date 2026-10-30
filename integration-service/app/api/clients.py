from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.clients.fineract import FineractError
from app.domain.clients import CLIENT_UID_PATTERN, FINERACT_FULLNAME_MAX_LENGTH, CreateClient, normalise_name
from app.services.clients import ClientService

router = APIRouter(tags=["clients"])

ClientUid = Annotated[str, Path(pattern=CLIENT_UID_PATTERN)]


class CreateClientIn(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    client_uid: str = Field(pattern=CLIENT_UID_PATTERN)
    name: str = Field(min_length=1, max_length=FINERACT_FULLNAME_MAX_LENGTH)
    activation_date: date | None = None

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        name = normalise_name(value)
        if not name:
            raise ValueError("name must not be blank")
        return name


class ClientOut(BaseModel):
    client_uid: str
    name: str
    active: bool
    status: str
    office: str
    activation_date: date | None


class ClientValidationOut(BaseModel):
    client_uid: str
    valid: bool
    exists: bool
    active: bool
    name_matches: bool


def _service(request: Request) -> ClientService:
    service: ClientService = request.app.state.clients
    return service


@router.post("/clients", status_code=status.HTTP_201_CREATED)
def create_client(body: CreateClientIn, request: Request) -> JSONResponse:
    result = _service(request).create(
        CreateClient(
            external_reference=body.external_reference,
            client_uid=body.client_uid,
            name=body.name,
            activation_date=body.activation_date,
        )
    )
    return JSONResponse(result.body, status_code=result.status_code)


@router.get("/clients/{client_uid}", response_model=ClientOut)
def get_client(client_uid: ClientUid, request: Request) -> ClientOut:
    try:
        record = _service(request).find(client_uid)
    except FineractError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Fineract is unavailable") from exc
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"client {client_uid} not found")
    return ClientOut(
        client_uid=client_uid,
        name=record.name,
        active=record.active,
        status=record.status,
        office=record.office_name,
        activation_date=record.activation_date,
    )


@router.get("/clients/{client_uid}/validation", response_model=ClientValidationOut)
def validate_client(
    client_uid: ClientUid,
    request: Request,
    name: Annotated[str, Query(min_length=1, max_length=FINERACT_FULLNAME_MAX_LENGTH)],
) -> ClientValidationOut:
    try:
        result = _service(request).validate(client_uid, name)
    except FineractError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Fineract is unavailable") from exc
    return ClientValidationOut(
        client_uid=result.client_uid,
        valid=result.valid,
        exists=result.exists,
        active=result.active,
        name_matches=result.name_matches,
    )
