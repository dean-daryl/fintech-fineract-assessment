from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.fineract import FineractClient
from app.config import Settings
from app.domain.clients import (
    ClientValidation,
    CreateClient,
    FineractClientRecord,
    names_match,
    to_fineract_client,
    validate_client,
)
from app.domain.states import OperationStatus, OperationType
from app.models import ClientMap, Operation
from app.services.pipeline import FineractOutcome, OperationPipeline, OperationRejected, OperationResult


class ClientCreation:
    operation_type = OperationType.CREATE_CLIENT

    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, settings: Settings) -> None:
        self._sessions = session_factory
        self._fineract = fineract
        self._settings = settings
        self._office_id: int | None = None

    def prepare(self, operation: Operation) -> None:
        client_uid: str = operation.payload["client_uid"]
        with self._sessions() as session, session.begin():
            session.execute(
                insert(ClientMap)
                .values(client_uid=client_uid, operation_id=operation.id)
                .on_conflict_do_nothing(index_elements=["client_uid"])
            )
            row = session.get_one(ClientMap, client_uid, with_for_update=True)
            if row.operation_id == operation.id:
                return
            owner_status = session.scalar(select(Operation.status).where(Operation.id == row.operation_id))
            if owner_status == OperationStatus.FAILED and row.fineract_client_id is None:
                row.operation_id = operation.id
                return
        raise OperationRejected(409, "client_uid is already registered by another request")

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        existing = self._fineract.find_client_by_external_id(operation.payload["client_uid"])
        if existing is None:
            return None
        if not names_match(operation.payload["name"], existing.name):
            raise OperationRejected(409, "a Fineract client with this client_uid exists under a different name")
        return FineractOutcome(existing.fineract_id)

    def execute(self, operation: Operation) -> FineractOutcome:
        payload = to_fineract_client(
            operation.payload["client_uid"],
            operation.payload["name"],
            self._activation_date(operation),
            self._office(),
        )
        return FineractOutcome(self._fineract.create_client(payload))

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        with self._sessions() as session, session.begin():
            session.execute(
                update(ClientMap)
                .where(ClientMap.client_uid == operation.payload["client_uid"], ClientMap.operation_id == operation.id)
                .values(fineract_client_id=outcome.resource_id)
            )

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"client_uid": operation.payload["client_uid"], "name": operation.payload["name"]}

    def _office(self) -> int:
        if self._office_id is None:
            office_id = self._fineract.find_office_id(self._settings.fineract_office_name)
            if office_id is None:
                raise RuntimeError(f"Fineract office {self._settings.fineract_office_name!r} not found; run make bootstrap")
            self._office_id = office_id
        return self._office_id

    def _activation_date(self, operation: Operation) -> date:
        requested: str | None = operation.payload.get("activation_date")
        if requested:
            return date.fromisoformat(requested)
        return datetime.now(ZoneInfo(self._settings.tenant_timezone)).date()


class ClientService:
    def __init__(self, pipeline: OperationPipeline, creation: ClientCreation, fineract: FineractClient) -> None:
        self._pipeline = pipeline
        self._creation = creation
        self._fineract = fineract

    @property
    def handlers(self) -> list[ClientCreation]:
        return [self._creation]

    def create(self, request: CreateClient) -> OperationResult:
        return self._pipeline.submit(self._creation, request.external_reference, request.payload())

    def find(self, client_uid: str) -> FineractClientRecord | None:
        return self._fineract.find_client_by_external_id(client_uid)

    def validate(self, client_uid: str, expected_name: str) -> ClientValidation:
        return validate_client(client_uid, self.find(client_uid), expected_name)
