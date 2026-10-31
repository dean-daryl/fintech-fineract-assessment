import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.clients.erp import ErpClient, ErpError, ErpJournalEntry, ErpRejectedError, ErpTimeoutError, ErpUnavailableError
from app.clients.fineract import FineractError, FineractRejectedError, FineractTimeoutError, FineractUnavailableError
from app.config import Settings
from app.domain.accounting import AccountingMappingError, JournalEntry, to_erp_journal_entry
from app.domain.idempotency import ReplayKind, payload_hash, replay_kind
from app.domain.loans import FineractLoanTransaction
from app.domain.states import OperationStatus, OperationType, has_erp_step
from app.faults import FaultInjector, FaultPoint
from app.models import Operation
from app.services.operations import OperationStore

log = logging.getLogger(__name__)

RECOVERY_BATCH_SIZE = 100
FINERACT_NOT_AUTHORIZED = "error.msg.not.authorized"


@dataclass(frozen=True)
class OperationResult:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class FineractOutcome:
    resource_id: int | None
    details: dict[str, Any] = field(default_factory=dict)
    transaction: FineractLoanTransaction | None = None


class OperationRejected(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class OperationHandler(Protocol):
    operation_type: OperationType

    def prepare(self, operation: Operation) -> None: ...

    def look_up(self, operation: Operation) -> FineractOutcome | None: ...

    def execute(self, operation: Operation) -> FineractOutcome: ...

    def record(self, operation: Operation, outcome: FineractOutcome) -> None: ...

    def journal_entry(self, operation: Operation, outcome: FineractOutcome) -> JournalEntry | None: ...

    def describe(self, operation: Operation) -> dict[str, Any]: ...


def rejection_status_code(status_code: int, code: str | None) -> int:
    if code == FINERACT_NOT_AUTHORIZED:
        return 502
    if status_code == 403:
        return 409
    if status_code == 404:
        return 404
    return 422


class OperationPipeline:
    def __init__(self, store: OperationStore, erp: ErpClient, settings: Settings, faults: FaultInjector | None = None) -> None:
        self._store = store
        self._erp = erp
        self._settings = settings
        self._faults = faults or FaultInjector(enabled=False)

    def submit(self, handler: OperationHandler, external_reference: str, payload: dict[str, Any]) -> OperationResult:
        claim = self._store.claim(handler.operation_type, external_reference, payload)
        if not claim.created:
            return self._replay(handler, claim.operation, payload_hash(payload))
        return self._run(handler, claim.operation, look_up_first=False)

    def recover(self, handlers: Sequence[OperationHandler]) -> int:
        by_type = {handler.operation_type: handler for handler in handlers}
        operations = self._store.claim_due(list(by_type), RECOVERY_BATCH_SIZE)
        for operation in operations:
            try:
                self._run(by_type[OperationType(operation.operation_type)], operation, look_up_first=True)
            except Exception:
                log.exception("Operation recovery failed", extra=_log_fields(operation))
        return len(operations)

    def _replay(self, handler: OperationHandler, operation: Operation, request_hash: str) -> OperationResult:
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
            return OperationResult(202, self._body(handler, operation, OperationStatus(operation.status)))
        assert operation.response is not None and operation.response_code is not None
        return OperationResult(200 if operation.response_code == 201 else operation.response_code, operation.response)

    def _run(self, handler: OperationHandler, operation: Operation, look_up_first: bool) -> OperationResult:
        status = OperationStatus(operation.status)

        if status is OperationStatus.RECEIVED:
            try:
                if self._faults.fires(FaultPoint.FINERACT_DOWN, operation.external_reference):
                    raise FineractUnavailableError("injected fault: Fineract is down")
                handler.prepare(operation)
            except OperationRejected as exc:
                return self._fail(handler, operation, exc.status_code, str(exc))
            except (FineractUnavailableError, FineractTimeoutError) as exc:
                return self._retry_later(handler, operation, exc)
            except FineractRejectedError as exc:
                return self._fail(handler, operation, rejection_status_code(exc.status_code, exc.code), exc.message)
            operation = self._store.transition(operation.id, OperationStatus.FINERACT_PENDING)
            status = OperationStatus.FINERACT_PENDING

        if status is OperationStatus.FINERACT_PENDING:
            try:
                outcome = self._push(handler, operation, look_up_first)
            except OperationRejected as exc:
                return self._fail(handler, operation, exc.status_code, str(exc))
            except FineractRejectedError as exc:
                return self._rejected(handler, operation, exc)
            except (FineractUnavailableError, FineractTimeoutError) as exc:
                return self._retry_later(handler, operation, exc)
            return self._complete(handler, operation, outcome)

        return self._finish(handler, operation)

    def _push(self, handler: OperationHandler, operation: Operation, look_up_first: bool) -> FineractOutcome:
        look_up = look_up_first
        last_error: FineractError | None = None
        for attempt in range(self._settings.operation_inline_attempts):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            try:
                if look_up:
                    existing = handler.look_up(operation)
                    if existing is not None:
                        return existing
                outcome = handler.execute(operation)
                if self._faults.fires(FaultPoint.FINERACT_TIMEOUT_AFTER_SEND, operation.external_reference):
                    raise FineractTimeoutError("injected fault: the response was lost after Fineract accepted the request")
                return outcome
            except FineractUnavailableError as exc:
                last_error = exc
            except FineractTimeoutError as exc:
                last_error = exc
                look_up = True
            log.warning("Fineract attempt failed", extra=_log_fields(operation))
        assert last_error is not None
        raise last_error

    def _rejected(self, handler: OperationHandler, operation: Operation, error: FineractRejectedError) -> OperationResult:
        try:
            existing = handler.look_up(operation)
        except OperationRejected as exc:
            return self._fail(handler, operation, exc.status_code, str(exc))
        except FineractError:
            existing = None
        if existing is not None:
            return self._complete(handler, operation, existing)
        return self._fail(
            handler, operation, rejection_status_code(error.status_code, error.code), f"Fineract rejected the request: {error.message}"
        )

    def _retry_later(self, handler: OperationHandler, operation: Operation, error: FineractError) -> OperationResult:
        if operation.attempts + 1 < self._settings.operation_max_attempts:
            operation = self._store.defer(operation.id, str(error))
            return OperationResult(202, self._body(handler, operation, OperationStatus(operation.status)))
        if OperationStatus(operation.status) is OperationStatus.RECEIVED:
            return self._fail(handler, operation, 503, f"Fineract was unavailable on every attempt: {error}")
        try:
            existing = handler.look_up(operation)
        except OperationRejected as exc:
            return self._fail(handler, operation, exc.status_code, str(exc))
        except FineractError:
            body = self._body(handler, operation, OperationStatus.NEEDS_RECONCILIATION)
            self._store.transition(
                operation.id,
                OperationStatus.NEEDS_RECONCILIATION,
                last_error=str(error),
                response_code=202,
                response=body,
            )
            return OperationResult(202, body)
        if existing is None:
            return self._fail(handler, operation, 503, f"Fineract did not accept the request after every attempt: {error}")
        return self._complete(handler, operation, existing)

    def _complete(self, handler: OperationHandler, operation: Operation, outcome: FineractOutcome) -> OperationResult:
        handler.record(operation, outcome)
        erp_request: dict[str, Any] | None = None
        mapping_error: str | None = None
        if has_erp_step(handler.operation_type):
            try:
                entry = handler.journal_entry(operation, outcome)
                if entry is None:
                    raise AccountingMappingError("the handler built no journal entry")
                erp_request = to_erp_journal_entry(entry)
            except AccountingMappingError as exc:
                mapping_error = f"no journal entry for the Fineract transaction: {exc}"
        operation = self._store.transition(
            operation.id,
            OperationStatus.FINERACT_DONE,
            fineract_resource_id=outcome.resource_id,
            erp_request=erp_request,
            response=outcome.details,
        )
        if mapping_error is not None:
            return self._reconcile(handler, operation, mapping_error)
        return self._finish(handler, operation)

    def _finish(self, handler: OperationHandler, operation: Operation) -> OperationResult:
        status = OperationStatus(operation.status)
        if has_erp_step(handler.operation_type):
            look_up_first = status is OperationStatus.ERP_PENDING
            if status is OperationStatus.FINERACT_DONE:
                operation = self._store.transition(operation.id, OperationStatus.ERP_PENDING, reset_attempts=True)
                self._faults.crash_if_armed(operation.external_reference)
            return self._post_to_erp(handler, operation, look_up_first)
        body = {**self._body(handler, operation, OperationStatus.COMPLETED), **(operation.response or {})}
        self._store.transition(operation.id, OperationStatus.COMPLETED, response_code=201, response=body)
        return OperationResult(201, body)

    def _post_to_erp(self, handler: OperationHandler, operation: Operation, look_up_first: bool) -> OperationResult:
        request = operation.erp_request
        if request is None:
            return self._reconcile(handler, operation, "no journal entry was stored for this operation")
        try:
            entry = self._push_to_erp(operation, request, look_up_first)
        except ErpRejectedError as exc:
            return self._reconcile(handler, operation, f"the ERP rejected the journal entry: {exc.detail}")
        except (ErpUnavailableError, ErpTimeoutError) as exc:
            return self._erp_retry_later(handler, operation, exc)
        if entry.request != request:
            return self._reconcile(handler, operation, "the ERP holds a different journal entry under this reference")
        body = {
            **self._body(handler, operation, OperationStatus.COMPLETED),
            **(operation.response or {}),
            "erp_transaction_id": entry.erp_transaction_id,
        }
        self._store.transition(
            operation.id,
            OperationStatus.COMPLETED,
            erp_transaction_id=entry.erp_transaction_id,
            response_code=201,
            response=body,
        )
        return OperationResult(201, body)

    def _push_to_erp(self, operation: Operation, request: dict[str, Any], look_up_first: bool) -> ErpJournalEntry:
        look_up = look_up_first
        last_error: ErpError | None = None
        for attempt in range(self._settings.operation_inline_attempts):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            try:
                if look_up:
                    existing = self._erp.find_journal_entry(request["external_reference"])
                    if existing is not None:
                        return existing
                entry = self._erp.post_journal_entry(request)
                if self._faults.fires(FaultPoint.ERP_TIMEOUT_AFTER_WRITE, operation.external_reference):
                    raise ErpTimeoutError("injected fault: the response was lost after the ERP wrote the entry")
                return entry
            except ErpUnavailableError as exc:
                last_error = exc
            except ErpTimeoutError as exc:
                last_error = exc
                look_up = True
            log.warning("ERP attempt failed", extra=_log_fields(operation))
        assert last_error is not None
        raise last_error

    def _erp_retry_later(self, handler: OperationHandler, operation: Operation, error: ErpError) -> OperationResult:
        if operation.attempts + 1 < self._settings.operation_max_attempts:
            operation = self._store.defer(operation.id, str(error))
            return OperationResult(202, {**self._body(handler, operation, OperationStatus.ERP_PENDING), **(operation.response or {})})
        return self._reconcile(handler, operation, f"the ERP did not accept the journal entry after every attempt: {error}")

    def _reconcile(self, handler: OperationHandler, operation: Operation, error: str) -> OperationResult:
        body = {**self._body(handler, operation, OperationStatus.NEEDS_RECONCILIATION), **(operation.response or {}), "error": error}
        self._store.transition(
            operation.id,
            OperationStatus.NEEDS_RECONCILIATION,
            last_error=error,
            response_code=202,
            response=body,
        )
        return OperationResult(202, body)

    def _fail(self, handler: OperationHandler, operation: Operation, status_code: int, error: str) -> OperationResult:
        body = {**self._body(handler, operation, OperationStatus.FAILED), "error": error}
        self._store.transition(operation.id, OperationStatus.FAILED, last_error=error, response_code=status_code, response=body)
        return OperationResult(status_code, body)

    @staticmethod
    def _body(handler: OperationHandler, operation: Operation, status: OperationStatus) -> dict[str, Any]:
        return {"external_reference": operation.external_reference, **handler.describe(operation), "operation_status": status.value}


def _log_fields(operation: Operation) -> dict[str, Any]:
    return {
        "external_reference": operation.external_reference,
        "client_uid": operation.payload.get("client_uid"),
        "loan_reference": operation.payload.get("loan_reference"),
        "erp_transaction_id": operation.erp_transaction_id,
        "status": operation.status,
    }
