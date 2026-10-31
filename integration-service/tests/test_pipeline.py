from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.clients.erp import ErpJournalEntry, ErpRejectedError, ErpTimeoutError, ErpUnavailableError
from app.clients.fineract import FineractRejectedError, FineractTimeoutError, FineractUnavailableError
from app.config import Settings
from app.domain.accounting import AccountingMappingError, JournalEntry, JournalLine, to_erp_journal_entry
from app.domain.idempotency import payload_hash
from app.domain.states import OperationStatus, OperationType, check_transition, is_finished
from app.faults import FaultInjector, FaultPoint
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

    def transition(self, operation_id: int, target: OperationStatus, reset_attempts: bool = False, **changes: Any) -> Operation:
        row = self._row(operation_id)
        check_transition(OperationType(row.operation_type), OperationStatus(row.status), target)
        row.status = target.value
        if reset_attempts:
            row.attempts = 0
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

    def claim_due(self, operation_types: Collection[OperationType], limit: int) -> list[Operation]:
        return [row for row in self.rows.values() if not is_finished(OperationStatus(row.status))][:limit]


class Crash(BaseException):
    pass


class FakeErp:
    def __init__(self, post_script: list[str] | None = None) -> None:
        self.entries: dict[str, ErpJournalEntry] = {}
        self.post_script = post_script or []
        self.down = False
        self.posted = 0
        self.looked_up = 0

    def post_journal_entry(self, request: dict[str, Any]) -> ErpJournalEntry:
        self.posted += 1
        action = self.post_script.pop(0) if self.post_script else "ok"
        if self.down or action == "down":
            raise ErpUnavailableError("connection refused")
        if action == "crash":
            raise Crash
        if action == "reject":
            raise ErpRejectedError(422, "entry is unbalanced")
        reference = request["external_reference"]
        existing = self.entries.get(reference)
        if existing is not None and existing.request != request:
            raise ErpRejectedError(409, f"journal entry {reference} already exists with different details")
        entry = existing or ErpJournalEntry(f"JE-{len(self.entries) + 1:06d}", request)
        self.entries[reference] = entry
        if action == "timeout_after_write":
            raise ErpTimeoutError("read timeout")
        return entry

    def find_journal_entry(self, external_reference: str) -> ErpJournalEntry | None:
        self.looked_up += 1
        if self.down:
            raise ErpUnavailableError("connection refused")
        return self.entries.get(external_reference)


class FakeHandler:
    operation_type = OperationType.REPAY_LOAN

    def __init__(self, execute: list[Exception | FineractOutcome], look_up: list[Exception | FineractOutcome | None]) -> None:
        self._execute = execute
        self._look_up = look_up
        self.executed = 0
        self.looked_up = 0
        self.mapping_error: AccountingMappingError | None = None

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

    def journal_entry(self, operation: Operation, outcome: FineractOutcome) -> JournalEntry | None:
        if self.mapping_error is not None:
            raise self.mapping_error
        return JournalEntry(
            external_reference=operation.external_reference,
            entry_date=date(2026, 5, 1),
            description="Loan LN-2026-000001 repayment",
            currency="RWF",
            lines=(JournalLine("1100", debit=458403), JournalLine("1200", credit=383403), JournalLine("4100", credit=75000)),
        )

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"loan_reference": "LN-2026-000001"}


DONE = FineractOutcome(31, {"principal_portion": 383403, "interest_portion": 75000})
PAYLOAD = {"loan_reference": "LN-2026-000001", "amount": 458403}


def _pipeline(erp: FakeErp | None = None, faults: FaultInjector | None = None, **settings: Any) -> tuple[OperationPipeline, FakeStore]:
    store = FakeStore()
    config = Settings(
        database_url="x",
        erp_base_url="x",
        fineract_base_url="x",
        fineract_username="x",
        fineract_password="x",
        **settings,
    )
    return OperationPipeline(store, erp or FakeErp(), config, faults), store  # type: ignore[arg-type]


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


def _row(store: FakeStore, reference: str = "REPAY-1") -> Operation:
    return store.rows[("REPAY_LOAN", reference)]


def test_completed_repayment_has_one_erp_entry_built_before_the_erp_call() -> None:
    erp = FakeErp()
    pipeline, store = _pipeline(erp)
    handler = FakeHandler(execute=[DONE], look_up=[])
    first = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    again = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    row = _row(store)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.body["erp_transaction_id"] == again.body["erp_transaction_id"] == "JE-000001"
    assert (row.status, row.erp_transaction_id) == ("COMPLETED", "JE-000001")
    assert row.erp_request == erp.entries["REPAY-1"].request
    assert row.erp_request["lines"][1:] == [
        {"account_code": "1200", "debit": 0, "credit": 383403},
        {"account_code": "4100", "debit": 0, "credit": 75000},
    ]
    assert (erp.posted, erp.looked_up) == (1, 0)


def test_erp_down_after_fineract_success_is_deferred_then_recovered() -> None:
    erp = FakeErp()
    erp.down = True
    pipeline, store = _pipeline(erp, operation_inline_attempts=2)
    handler = FakeHandler(execute=[DONE], look_up=[])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert result.status_code == 202
    assert (result.body["operation_status"], result.body["interest_portion"]) == ("ERP_PENDING", 75000)
    assert (_row(store).status, _row(store).attempts) == ("ERP_PENDING", 1)
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 202

    erp.down = False
    assert pipeline.recover([handler]) == 1
    assert _row(store).status == "COMPLETED"
    assert len(erp.entries) == 1
    assert handler.executed == 1
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).body["erp_transaction_id"] == "JE-000001"


def test_erp_timeout_after_write_is_followed_by_a_look_up_not_a_second_post() -> None:
    erp = FakeErp(post_script=["timeout_after_write"])
    pipeline, store = _pipeline(erp)
    result = pipeline.submit(FakeHandler(execute=[DONE], look_up=[]), "REPAY-1", PAYLOAD)
    assert (result.status_code, result.body["erp_transaction_id"]) == (201, "JE-000001")
    assert (erp.posted, erp.looked_up, len(erp.entries)) == (1, 1, 1)


def test_crash_before_the_erp_answers_is_completed_by_recovery() -> None:
    erp = FakeErp(post_script=["crash"])
    pipeline, store = _pipeline(erp)
    handler = FakeHandler(execute=[DONE], look_up=[])
    with pytest.raises(Crash):
        pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert _row(store).status == "ERP_PENDING"

    pipeline.recover([handler])
    assert _row(store).status == "COMPLETED"
    assert (erp.looked_up, len(erp.entries), handler.executed) == (1, 1, 1)


def test_crash_right_after_fineract_done_is_completed_by_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    erp = FakeErp()
    pipeline, store = _pipeline(erp)
    handler = FakeHandler(execute=[DONE], look_up=[])
    transition = store.transition

    def crash_on_erp_pending(operation_id: int, target: OperationStatus, **changes: Any) -> Operation:
        if target is OperationStatus.ERP_PENDING:
            raise Crash
        return transition(operation_id, target, **changes)

    monkeypatch.setattr(store, "transition", crash_on_erp_pending)
    with pytest.raises(Crash):
        pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (_row(store).status, erp.posted) == ("FINERACT_DONE", 0)
    assert _row(store).erp_request is not None

    monkeypatch.setattr(store, "transition", transition)
    pipeline.recover([handler])
    assert _row(store).status == "COMPLETED"
    assert (erp.posted, handler.executed) == (1, 1)


@pytest.mark.parametrize("script", [["reject"], []])
def test_erp_rejection_needs_reconciliation_not_failure(script: list[str]) -> None:
    erp = FakeErp(post_script=script)
    if not script:
        other = {"external_reference": "REPAY-1", "lines": []}
        erp.entries["REPAY-1"] = ErpJournalEntry("JE-000009", other)
    pipeline, store = _pipeline(erp)
    result = pipeline.submit(FakeHandler(execute=[DONE], look_up=[]), "REPAY-1", PAYLOAD)
    assert result.status_code == 202
    assert result.body["operation_status"] == "NEEDS_RECONCILIATION"
    assert "ERP rejected" in result.body["error"]
    assert _row(store).status == "NEEDS_RECONCILIATION"


def test_different_entry_found_by_look_up_needs_reconciliation() -> None:
    erp = FakeErp()
    erp.down = True
    pipeline, store = _pipeline(erp, operation_inline_attempts=1)
    handler = FakeHandler(execute=[DONE], look_up=[])
    pipeline.submit(handler, "REPAY-1", PAYLOAD)
    erp.down = False
    erp.entries["REPAY-1"] = ErpJournalEntry("JE-000009", {"external_reference": "REPAY-1", "lines": []})
    posted_while_down = erp.posted
    pipeline.recover([handler])
    row = _row(store)
    assert (row.status, row.last_error) == ("NEEDS_RECONCILIATION", "the ERP holds a different journal entry under this reference")
    assert erp.posted == posted_while_down


def test_mapping_error_needs_reconciliation_without_calling_the_erp() -> None:
    erp = FakeErp()
    pipeline, store = _pipeline(erp)
    handler = FakeHandler(execute=[DONE], look_up=[])
    handler.mapping_error = AccountingMappingError("repayment portions add up to 1, not to the transaction amount 2")
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (result.status_code, result.body["operation_status"]) == (202, "NEEDS_RECONCILIATION")
    assert result.body["interest_portion"] == 75000
    assert _row(store).fineract_resource_id == 31
    assert (erp.posted, erp.looked_up) == (0, 0)


def test_erp_attempts_start_from_zero_and_end_in_reconciliation() -> None:
    erp = FakeErp()
    erp.down = True
    pipeline, store = _pipeline(erp, operation_inline_attempts=1, operation_max_attempts=2)
    handler = FakeHandler(execute=[FineractUnavailableError("down"), DONE], look_up=[None])
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).body["operation_status"] == "FINERACT_PENDING"
    assert _row(store).attempts == 1

    pipeline.recover([handler])
    assert (_row(store).status, _row(store).attempts) == ("ERP_PENDING", 1)

    pipeline.recover([handler])
    row = _row(store)
    assert row.status == "NEEDS_RECONCILIATION"
    assert row.last_error is not None and row.last_error.startswith("the ERP did not accept the journal entry after every attempt")
    assert handler.executed == 2


def test_journal_entry_request_matches_the_erp_api() -> None:
    entry = FakeHandler(execute=[], look_up=[]).journal_entry(Operation(external_reference="REPAY-1"), DONE)
    assert entry is not None
    assert to_erp_journal_entry(entry)["external_reference"] == "REPAY-1"


def _crash() -> None:
    raise Crash


def test_injected_fineract_down_persists_the_request_and_executes_nothing() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 1)
    pipeline, store = _pipeline(faults=faults)
    handler = FakeHandler(execute=[DONE], look_up=[None])
    result = pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (result.status_code, result.body["operation_status"]) == (202, "RECEIVED")
    assert (_row(store).attempts, _row(store).last_error) == (1, "injected fault: Fineract is down")
    assert handler.executed == 0

    pipeline.recover([handler])
    assert _row(store).status == "COMPLETED"
    assert handler.executed == 1


def test_injected_fineract_timeout_after_send_is_resolved_by_look_up() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.FINERACT_TIMEOUT_AFTER_SEND, "REPAY-1", 1)
    pipeline, store = _pipeline(faults=faults)
    handler = FakeHandler(execute=[DONE], look_up=[DONE])
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 201
    assert (handler.executed, handler.looked_up) == (1, 1)


def test_injected_erp_timeout_after_write_leaves_one_entry() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.ERP_TIMEOUT_AFTER_WRITE, "REPAY-1", 1)
    erp = FakeErp()
    pipeline, store = _pipeline(erp, faults)
    assert pipeline.submit(FakeHandler(execute=[DONE], look_up=[]), "REPAY-1", PAYLOAD).status_code == 201
    assert (erp.posted, erp.looked_up, len(erp.entries)) == (1, 1, 1)


def test_injected_crash_happens_after_erp_pending_is_saved() -> None:
    faults = FaultInjector(enabled=True, crash=_crash)
    faults.arm(FaultPoint.CRASH_BEFORE_ERP, "REPAY-1", 1)
    erp = FakeErp()
    pipeline, store = _pipeline(erp, faults)
    handler = FakeHandler(execute=[DONE], look_up=[])
    with pytest.raises(Crash):
        pipeline.submit(handler, "REPAY-1", PAYLOAD)
    assert (_row(store).status, _row(store).erp_request is not None, erp.posted) == ("ERP_PENDING", True, 0)

    pipeline.recover([handler])
    assert _row(store).status == "COMPLETED"
    assert (erp.looked_up, erp.posted, handler.executed) == (1, 1, 1)


def test_fineract_down_on_every_attempt_fails_with_503_and_nothing_sent() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 10)
    pipeline, store = _pipeline(faults=faults, operation_max_attempts=2)
    handler = FakeHandler(execute=[DONE], look_up=[])
    assert pipeline.submit(handler, "REPAY-1", PAYLOAD).status_code == 202
    pipeline.recover([handler])
    assert (_row(store).status, _row(store).response_code) == ("FAILED", 503)
    assert (handler.executed, handler.looked_up) == (0, 0)
