import pytest

from app.domain.states import (
    TRANSITIONS,
    InvalidTransitionError,
    OperationStatus,
    OperationType,
    check_transition,
    has_erp_step,
    is_finished,
)

S = OperationStatus
CLIENT = OperationType.CREATE_CLIENT


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.FINERACT_PENDING),
        (S.RECEIVED, S.FAILED),
        (S.FINERACT_PENDING, S.FINERACT_DONE),
        (S.FINERACT_PENDING, S.FAILED),
        (S.FINERACT_PENDING, S.NEEDS_RECONCILIATION),
        (S.FINERACT_DONE, S.COMPLETED),
    ],
)
def test_client_operation_allows(current: OperationStatus, target: OperationStatus) -> None:
    check_transition(CLIENT, current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.FINERACT_DONE),
        (S.RECEIVED, S.COMPLETED),
        (S.FINERACT_PENDING, S.COMPLETED),
        (S.FINERACT_DONE, S.ERP_PENDING),
        (S.FINERACT_DONE, S.FAILED),
        (S.COMPLETED, S.FAILED),
        (S.FAILED, S.FINERACT_PENDING),
        (S.NEEDS_RECONCILIATION, S.COMPLETED),
        (S.RECEIVED, S.RECEIVED),
    ],
)
def test_client_operation_rejects(current: OperationStatus, target: OperationStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(CLIENT, current, target)


def test_every_status_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(OperationStatus)


@pytest.mark.parametrize("status", list(OperationStatus))
def test_finished_statuses_have_no_way_out(status: OperationStatus) -> None:
    assert is_finished(status) == (not TRANSITIONS[status])


def test_gl_sync_operation_completes_without_erp_step() -> None:
    check_transition(OperationType.SYNC_GL_ACCOUNTS, S.FINERACT_DONE, S.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        check_transition(OperationType.SYNC_GL_ACCOUNTS, S.FINERACT_DONE, S.ERP_PENDING)


@pytest.mark.parametrize("operation_type", [OperationType.DISBURSE_LOAN, OperationType.REPAY_LOAN])
def test_money_movements_must_go_through_the_erp_step(operation_type: OperationType) -> None:
    assert has_erp_step(operation_type)
    check_transition(operation_type, S.FINERACT_DONE, S.ERP_PENDING)
    check_transition(operation_type, S.FINERACT_DONE, S.NEEDS_RECONCILIATION)
    check_transition(operation_type, S.ERP_PENDING, S.COMPLETED)
    check_transition(operation_type, S.ERP_PENDING, S.NEEDS_RECONCILIATION)
    with pytest.raises(InvalidTransitionError):
        check_transition(operation_type, S.FINERACT_DONE, S.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        check_transition(operation_type, S.ERP_PENDING, S.FAILED)


@pytest.mark.parametrize("operation_type", [OperationType.CREATE_CLIENT, OperationType.CREATE_LOAN, OperationType.APPROVE_LOAN])
def test_operations_without_money_movement_cannot_need_an_erp_entry(operation_type: OperationType) -> None:
    assert not has_erp_step(operation_type)
    with pytest.raises(InvalidTransitionError):
        check_transition(operation_type, S.FINERACT_DONE, S.NEEDS_RECONCILIATION)
