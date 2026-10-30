"""Operation state machine. Pure functions, no I/O."""

from enum import StrEnum


class OperationType(StrEnum):
    CREATE_CLIENT = "CREATE_CLIENT"


class OperationStatus(StrEnum):
    RECEIVED = "RECEIVED"
    FINERACT_PENDING = "FINERACT_PENDING"
    FINERACT_DONE = "FINERACT_DONE"
    ERP_PENDING = "ERP_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.RECEIVED: frozenset({OperationStatus.FINERACT_PENDING, OperationStatus.FAILED}),
    OperationStatus.FINERACT_PENDING: frozenset(
        {OperationStatus.FINERACT_DONE, OperationStatus.FAILED, OperationStatus.NEEDS_RECONCILIATION}
    ),
    OperationStatus.FINERACT_DONE: frozenset({OperationStatus.ERP_PENDING, OperationStatus.COMPLETED}),
    OperationStatus.ERP_PENDING: frozenset({OperationStatus.COMPLETED, OperationStatus.NEEDS_RECONCILIATION}),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.NEEDS_RECONCILIATION: frozenset(),
}

OPERATIONS_WITHOUT_ERP_STEP: frozenset[OperationType] = frozenset({OperationType.CREATE_CLIENT})

FINISHED_STATUSES: frozenset[OperationStatus] = frozenset(
    {OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.NEEDS_RECONCILIATION}
)


class InvalidTransitionError(Exception):
    def __init__(self, operation_type: OperationType, current: OperationStatus, target: OperationStatus) -> None:
        super().__init__(f"{operation_type}: {current} -> {target} is not allowed")


def check_transition(operation_type: OperationType, current: OperationStatus, target: OperationStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(operation_type, current, target)
    has_erp_step = operation_type not in OPERATIONS_WITHOUT_ERP_STEP
    if current is OperationStatus.FINERACT_DONE and (target is OperationStatus.ERP_PENDING) != has_erp_step:
        raise InvalidTransitionError(operation_type, current, target)


def is_finished(status: OperationStatus) -> bool:
    return status in FINISHED_STATUSES
