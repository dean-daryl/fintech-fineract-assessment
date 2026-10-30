import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.fineract import (
    FineractClient,
    FineractError,
    FineractRejectedError,
    FineractTimeoutError,
    FineractUnavailableError,
)
from app.config import Settings
from app.domain.clients import (
    FINERACT_DUPLICATE_EXTERNAL_ID,
    ClientValidation,
    CreateClient,
    FineractClientRecord,
    names_match,
    to_fineract_client,
    validate_client,
)
from app.domain.idempotency import ReplayKind, payload_hash, replay_kind
from app.domain.states import OperationStatus, OperationType
from app.models import ClientMap, Operation
from app.services.operations import OperationStore

log = logging.getLogger(__name__)

RECOVERY_BATCH_SIZE = 100


@dataclass(frozen=True)
class OperationResult:
    status_code: int
    body: dict[str, Any]


class ClientConflictError(Exception):
    pass


class ClientService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: OperationStore,
        fineract: FineractClient,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._store = store
        self._fineract = fineract
        self._settings = settings
        self._office_id: int | None = None

    def create(self, request: CreateClient) -> OperationResult:
        payload = request.payload()
        claim = self._store.claim(OperationType.CREATE_CLIENT, request.external_reference, payload)
        if not claim.created:
            return self._replay(claim.operation, payload_hash(payload))
        return self._run(claim.operation, look_up_first=False)

    def recover(self) -> int:
        operations = self._store.claim_due([OperationType.CREATE_CLIENT], RECOVERY_BATCH_SIZE)
        for operation in operations:
            try:
                self._run(operation, look_up_first=True)
            except Exception:
                log.exception("Client operation recovery failed", extra=_log_fields(operation))
        return len(operations)

    def find(self, client_uid: str) -> FineractClientRecord | None:
        return self._fineract.find_client_by_external_id(client_uid)

    def validate(self, client_uid: str, expected_name: str) -> ClientValidation:
        return validate_client(client_uid, self.find(client_uid), expected_name)

    def _replay(self, operation: Operation, request_hash: str) -> OperationResult:
        kind = replay_kind(OperationStatus(operation.status), operation.payload_hash, request_hash)
        if kind is ReplayKind.PAYLOAD_CONFLICT:
            return OperationResult(
                409,
                {
                    "external_reference": operation.external_reference,
                    "error": "external_reference was already used with a different payload",
                },
            )
        if kind is ReplayKind.IN_PROGRESS:
            return OperationResult(202, _body(operation, OperationStatus(operation.status)))
        assert operation.response is not None and operation.response_code is not None
        return OperationResult(200 if operation.response_code == 201 else operation.response_code, operation.response)

    def _run(self, operation: Operation, look_up_first: bool) -> OperationResult:
        status = OperationStatus(operation.status)

        if status is OperationStatus.RECEIVED:
            if not self._reserve(operation):
                return self._fail(operation, 409, "client_uid is already registered by another request")
            operation = self._store.transition(operation.id, OperationStatus.FINERACT_PENDING)
            status = OperationStatus.FINERACT_PENDING

        if status is OperationStatus.FINERACT_PENDING:
            try:
                fineract_id = self._create_in_fineract(operation, look_up_first)
            except ClientConflictError as exc:
                return self._fail(operation, 409, str(exc))
            except FineractRejectedError as exc:
                code = 409 if exc.code == FINERACT_DUPLICATE_EXTERNAL_ID else 422
                return self._fail(operation, code, f"Fineract rejected the client: {exc.message}")
            except (FineractUnavailableError, FineractTimeoutError) as exc:
                return self._retry_later(operation, exc)
            return self._complete(operation, fineract_id)

        return self._finish(operation)

    def _complete(self, operation: Operation, fineract_id: int) -> OperationResult:
        self._record_mapping(operation, fineract_id)
        operation = self._store.transition(operation.id, OperationStatus.FINERACT_DONE, fineract_resource_id=fineract_id)
        return self._finish(operation)

    def _finish(self, operation: Operation) -> OperationResult:
        operation = self._store.transition(
            operation.id,
            OperationStatus.COMPLETED,
            response_code=201,
            response=_body(operation, OperationStatus.COMPLETED),
        )
        assert operation.response is not None
        return OperationResult(201, operation.response)

    def _create_in_fineract(self, operation: Operation, look_up_first: bool) -> int:
        client_uid: str = operation.payload["client_uid"]
        look_up = look_up_first
        last_error: FineractError | None = None
        for attempt in range(self._settings.operation_inline_attempts):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            try:
                if look_up:
                    existing = self._fineract.find_client_by_external_id(client_uid)
                    if existing is not None:
                        return self._adopt(operation, existing)
                return self._fineract.create_client(
                    to_fineract_client(client_uid, operation.payload["name"], self._activation_date(operation), self._office())
                )
            except FineractUnavailableError as exc:
                last_error = exc
            except FineractTimeoutError as exc:
                last_error = exc
                look_up = True
            log.warning("Fineract client attempt failed", extra=_log_fields(operation))
        assert last_error is not None
        raise last_error

    def _adopt(self, operation: Operation, existing: FineractClientRecord) -> int:
        if not names_match(operation.payload["name"], existing.name):
            raise ClientConflictError("a Fineract client with this client_uid exists under a different name")
        return existing.fineract_id

    def _retry_later(self, operation: Operation, error: FineractError) -> OperationResult:
        if operation.attempts + 1 < self._settings.operation_max_attempts:
            operation = self._store.defer(operation.id, str(error))
            return OperationResult(202, _body(operation, OperationStatus(operation.status)))
        try:
            existing = self._fineract.find_client_by_external_id(operation.payload["client_uid"])
        except FineractError:
            operation = self._store.transition(
                operation.id,
                OperationStatus.NEEDS_RECONCILIATION,
                last_error=str(error),
                response_code=202,
                response=_body(operation, OperationStatus.NEEDS_RECONCILIATION),
            )
            assert operation.response is not None
            return OperationResult(202, operation.response)
        if existing is None:
            return self._fail(operation, 503, f"Fineract did not accept the client after every attempt: {error}")
        try:
            fineract_id = self._adopt(operation, existing)
        except ClientConflictError as exc:
            return self._fail(operation, 409, str(exc))
        return self._complete(operation, fineract_id)

    def _fail(self, operation: Operation, status_code: int, error: str) -> OperationResult:
        body = {**_body(operation, OperationStatus.FAILED), "error": error}
        self._store.transition(
            operation.id,
            OperationStatus.FAILED,
            last_error=error,
            response_code=status_code,
            response=body,
        )
        return OperationResult(status_code, body)

    def _reserve(self, operation: Operation) -> bool:
        client_uid: str = operation.payload["client_uid"]
        with self._sessions() as session, session.begin():
            session.execute(
                insert(ClientMap)
                .values(client_uid=client_uid, operation_id=operation.id)
                .on_conflict_do_nothing(index_elements=["client_uid"])
            )
            row = session.get_one(ClientMap, client_uid, with_for_update=True)
            if row.operation_id == operation.id:
                return True
            owner = session.get_one(Operation, row.operation_id)
            if owner.status == OperationStatus.FAILED and row.fineract_client_id is None:
                row.operation_id = operation.id
                return True
            return False

    def _record_mapping(self, operation: Operation, fineract_id: int) -> None:
        with self._sessions() as session, session.begin():
            session.execute(
                update(ClientMap)
                .where(ClientMap.client_uid == operation.payload["client_uid"], ClientMap.operation_id == operation.id)
                .values(fineract_client_id=fineract_id)
            )

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


def _body(operation: Operation, status: OperationStatus) -> dict[str, Any]:
    return {
        "external_reference": operation.external_reference,
        "client_uid": operation.payload["client_uid"],
        "name": operation.payload["name"],
        "operation_status": status.value,
    }


def _log_fields(operation: Operation) -> dict[str, Any]:
    return {
        "external_reference": operation.external_reference,
        "client_uid": operation.payload.get("client_uid"),
        "status": operation.status,
    }
