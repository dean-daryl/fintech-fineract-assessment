from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.domain.states import OperationStatus, OperationType
from app.errors import error_body
from app.models import Operation
from app.services.operations import OperationStore
from app.services.pipeline import OperationHandler

router = APIRouter(tags=["transactions"])

ExternalReference = Annotated[str, Path(min_length=1, max_length=100)]


class TransactionOut(BaseModel):
    external_reference: str
    operation_type: OperationType
    operation_status: OperationStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any]


def transaction_view(operation: Operation, described: dict[str, Any]) -> TransactionOut:
    status = OperationStatus(operation.status)
    result = {
        **(operation.response or {}),
        "external_reference": operation.external_reference,
        **described,
        "operation_status": status.value,
    }
    return TransactionOut(
        external_reference=operation.external_reference,
        operation_type=OperationType(operation.operation_type),
        operation_status=status,
        attempts=operation.attempts,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        result=result,
    )


@router.get(
    "/transactions/{external_reference}",
    response_model=TransactionOut,
    responses={404: {"description": "No operation with this reference"}, 409: {"description": "Several operation types use it"}},
)
def get_transaction(
    external_reference: ExternalReference,
    request: Request,
    operation_type: Annotated[OperationType | None, Query()] = None,
) -> TransactionOut | JSONResponse:
    store: OperationStore = request.app.state.store
    operations = [op for op in store.find_by_reference(external_reference) if operation_type is None or op.operation_type == operation_type]
    if not operations:
        return JSONResponse(error_body(404, f"no transaction {external_reference}", external_reference=external_reference), status_code=404)
    if len(operations) > 1:
        types = [op.operation_type for op in operations]
        message = f"{external_reference} is used by {len(types)} operation types; pass operation_type to choose one"
        body = error_body(409, message, code="ambiguous_reference", external_reference=external_reference, operation_types=types)
        return JSONResponse(body, status_code=409)
    operation = operations[0]
    handlers: dict[str, OperationHandler] = {handler.operation_type: handler for handler in request.app.state.handlers}
    return transaction_view(operation, handlers[operation.operation_type].describe(operation))
