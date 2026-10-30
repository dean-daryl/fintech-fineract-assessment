from datetime import UTC, datetime
from typing import Any

import pytest

from app.clients.fineract import FineractRejectedError, FineractTimeoutError, FineractUnavailableError
from app.config import Settings
from app.domain.idempotency import payload_hash
from app.domain.states import OperationStatus, OperationType, check_transition
from app.models import Operation
from app.services.operations import Claim
from app.services.pipeline import FineractOutcome, OperationPipeline, OperationRejected, rejection_status_code


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Operation] = {}

    def claim(self, operation_type: OperationType, external_reference: str, payload: dict[str, Any]) -> Claim:
        key = (operation_type.value, external_reference)
        if key in self.rows:
            return Claim(self.rows[key], created=False)
        row = Operation(
            id=len(self.rows) + 1,
            operation_type=operation_type.value,
            external_reference=external_reference,
            payload=payload,
            payload_hash=payload_hash(payload),
            status=OperationStatus.RECEIVED.value,
            attempts=0,
        )
        self.rows[key] = row
        return Claim(row, created=True)

    def _row(self, operation_id: int) -> Operation:
        return next(row for row in self.rows.values() if row.id == operation_id)

    def transition(self, operation_id: int, target: OperationStatus, **changes: Any) -> Operation:
        row = self._row(operation_id)
        check_transition(OperationType(row.operation_type), OperationStatus(row.status), target)
        row.status = target.value
        for name, value in changes.items():
            if value is not None:
                setattr(row, name, value)
        return row

    def defer(self, operation_id: int, error: str) -> Operation:
        row = self._row(operation_id)
        row.attempts += 1
        row.last_error = error
        row.next_attempt_at = datetime.now(UTC)
        return row


class FakeHandler:
    operation_type = OperationType.REPAY_LOAN

    def __init__(self, execute: list[Exception | FineractOutcome], look_up: list[Exception | FineractOutcome | None]) -> None:
        self._execute = execute
        self._look_up = look_up
        self.executed = 0
        self.looked_up = 0

    def prepare(self, operation: Operation) -> None:
        return None

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        self.looked_up += 1
        result = self._look_up.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def execute(self, operation: Operation) -> FineractOutcome:
        self.executed += 1
        result = self._execute.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        return None

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"loan_reference": "LN-2026-000001"}


DONE = FineractOutcome(31, {"principal_portion": 383403, "interest_portion": 75000})
PAYLOAD = {"loan_reference": "LN-2026-000001", "amount": 458403}


def _pipeline(**settings: Any) -> tuple[OperationPipeline, FakeStore]:
    store = FakeStore()
    config = Settings(
        database_url="x",
        erp_base_url="x",
        fineract_base_url="x",
        fineract_username="x",
        fineract_password="x",
        **settings,
    )
    return OperationPipeline(store, config), store  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.pipeline.time.sleep", lambda _: None)


def test_success_returns_fineract_details_and_replays_as_200() -> None:
    pipeline, _ = _pipeline()
    handler = FakeHandler(execute=[DONE], look_up=[])
    first = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    again = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.body == again.body
    assert first.body["operation_status"] == "COMPLETED"
    assert first.body["interest_portion"] == 75000
    assert handler.executed == 1


def test_different_payload_with_same_reference_is_409_and_not_executed() -> None:
    pipeline, _ = _pipeline()
    handler = FakeHandler(execute=[DONE], look_up=[])
    pipeline.submit(handler, "REPAY-1", PAYLOAD)
    conflict = pipeline.submit(handler, "REPAY-1", {**PAYLOAD, "amount": 1})
    assert conflict.status_code == 409
    assert handler.executed == 1


def test_timeout_is_followed_by_a_look_up_not_a_resend() -> None:
    pipeline, _ = _pipeline()
    handler = FakeHandler(execute=[FineractTimeoutError("slow")], look_up=[DONE])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert result.status_code == 201
    assert (handler.executed, handler.looked_up) == (1, 1)


def test_unavailable_is_resent_without_look_up() -> None:
    pipeline, _ = _pipeline()
    handler = FakeHandler(execute=[FineractUnavailableError("down"), DONE], look_up=[])
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 201
    assert (handler.executed, handler.looked_up) == (2, 0)


def test_still_unavailable_after_inline_attempts_is_deferred_with_202() -> None:
    pipeline, store = _pipeline(operation_inline_attempts=2)
    handler = FakeHandler(execute=[FineractUnavailableError("down")] * 2, look_up=[])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    row = store.rows[("REPAY_LOAN", "REPAY-1")]
    assert result.status_code == 202
    assert (row.status, row.attempts) == ("FINERACT_PENDING", 1)
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 202


def test_rejection_is_final_unless_the_resource_already_exists() -> None:
    pipeline, store = _pipeline()
    rejected = FineractRejectedError(403, {"userMessageGlobalisationCode": "error.msg.loan.not.active"})
    handler = FakeHandler(execute=[rejected], look_up=[None])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert result.status_code == 409
    assert store.rows[("REPAY_LOAN", "REPAY-1")].status == "FAILED"
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 409

    earlier = FakeHandler(execute=[rejected], look_up=[DONE])
    assert pipeline.submit(earlier, "REPAY-2", PAYLOAD).status_code == 201


def test_prepare_rejection_fails_before_fineract_is_called() -> None:
    pipeline, store = _pipeline()
    handler = FakeHandler(execute=[DONE], look_up=[])
    handler.prepare = lambda operation: (_ for _ in ()).throw(OperationRejected(422, "overpayment"))  # type: ignore[method-assign]
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (result.status_code, result.body["error"]) == (422, "overpayment")
    assert handler.executed == 0


def test_last_attempt_looks_up_then_fails_when_nothing_happened() -> None:
    pipeline, store = _pipeline(operation_inline_attempts=1, operation_max_attempts=1)
    handler = FakeHandler(execute=[FineractUnavailableError("down")], look_up=[None])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert result.status_code == 503
    assert store.rows[("REPAY_LOAN", "REPAY-1")].status == "FAILED"


def test_last_attempt_with_unknown_outcome_needs_reconciliation() -> None:
    pipeline, store = _pipeline(operation_inline_attempts=1, operation_max_attempts=1)
    handler = FakeHandler(execute=[FineractTimeoutError("slow")], look_up=[FineractTimeoutError("still slow")])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert result.status_code == 202
    assert store.rows[("REPAY_LOAN", "REPAY-1")].status == "NEEDS_RECONCILIATION"


@pytest.mark.parametrize(
    ("status_code", "code", "expected"),
    [(403, "error.msg.not.authorized", 502), (403, "error.msg.loan.not.active", 409), (404, None, 404), (400, None, 422)],
)
def test_rejection_status_codes(status_code: int, code: str | None, expected: int) -> None:
    assert rejection_status_code(status_code, code) == expected
