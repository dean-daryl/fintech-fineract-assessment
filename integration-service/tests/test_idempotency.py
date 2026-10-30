from app.domain.idempotency import ReplayKind, payload_hash, replay_kind
from app.domain.states import OperationStatus

HASH = payload_hash({"client_uid": "SME-000001", "name": "Acme Ltd"})


def test_hash_ignores_key_order() -> None:
    assert payload_hash({"name": "Acme Ltd", "client_uid": "SME-000001"}) == HASH


def test_hash_changes_with_any_value() -> None:
    assert payload_hash({"client_uid": "SME-000001", "name": "Acme Limited"}) != HASH


def test_finished_operation_with_same_payload_returns_stored_result() -> None:
    for status in (OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.NEEDS_RECONCILIATION):
        assert replay_kind(status, HASH, HASH) is ReplayKind.STORED_RESULT


def test_unfinished_operation_with_same_payload_is_in_progress() -> None:
    for status in (OperationStatus.RECEIVED, OperationStatus.FINERACT_PENDING, OperationStatus.FINERACT_DONE):
        assert replay_kind(status, HASH, HASH) is ReplayKind.IN_PROGRESS


def test_different_payload_conflicts_in_any_status() -> None:
    other = payload_hash({"client_uid": "SME-000002", "name": "Acme Ltd"})
    for status in OperationStatus:
        assert replay_kind(status, HASH, other) is ReplayKind.PAYLOAD_CONFLICT
