from app.domain.idempotency import ReplayKind, payload_hash, replay_kind
from app.domain.states import OperationStatus
from app.models import Operation
from app.services.operations import operation_log_fields

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


def test_operation_log_fields_carry_the_brief_fields_and_the_fineract_transaction_only_for_money() -> None:
    repayment = Operation(
        operation_type="REPAY_LOAN",
        external_reference="FIN-2026-000001",
        payload={"loan_reference": "LN-2026-000001", "amount": 500000},
        status="COMPLETED",
        fineract_resource_id=31,
        erp_transaction_id="JE-000002",
    )
    assert operation_log_fields(repayment) == {
        "external_reference": "FIN-2026-000001",
        "client_uid": None,
        "loan_reference": "LN-2026-000001",
        "fineract_transaction_id": 31,
        "erp_transaction_id": "JE-000002",
        "status": "COMPLETED",
    }
    client = Operation(
        operation_type="CREATE_CLIENT",
        external_reference="C-1",
        payload={"client_uid": "SME-000001"},
        status="COMPLETED",
        fineract_resource_id=7,
    )
    assert operation_log_fields(client)["fineract_transaction_id"] is None
    assert operation_log_fields(client)["client_uid"] == "SME-000001"
