from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.transactions import transaction_view
from app.domain.states import OperationType
from app.main import app
from app.models import Operation

PARTNER = {"X-API-Key": "test-partner-key"}
AT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def operation(operation_type: OperationType, status: str, response: dict[str, Any] | None = None, reference: str = "REF-1") -> Operation:
    return Operation(
        operation_type=operation_type.value,
        external_reference=reference,
        payload={"loan_reference": "LN-2026-000001", "amount": 500000},
        payload_hash="h",
        status=status,
        attempts=1,
        response=response,
        created_at=AT,
        updated_at=AT,
    )


class FakeStore:
    def __init__(self, operations: list[Operation]) -> None:
        self.operations = operations

    def find_by_reference(self, external_reference: str) -> list[Operation]:
        return [op for op in self.operations if op.external_reference == external_reference]


class FakeHandler:
    def __init__(self, operation_type: OperationType) -> None:
        self.operation_type = operation_type

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {"loan_reference": operation.payload["loan_reference"]}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.state.handlers = [FakeHandler(t) for t in OperationType]
    yield TestClient(app)


def test_finished_transaction_returns_the_stored_result() -> None:
    stored = {
        "external_reference": "REF-1",
        "loan_reference": "LN-2026-000001",
        "operation_status": "COMPLETED",
        "principal_portion": 383400,
    }
    view = transaction_view(operation(OperationType.REPAY_LOAN, "COMPLETED", stored), {"loan_reference": "LN-2026-000001"})
    assert (view.operation_type, view.operation_status, view.attempts) == ("REPAY_LOAN", "COMPLETED", 1)
    assert view.result == stored


def test_in_progress_transaction_reports_its_current_status_over_an_older_stored_body() -> None:
    stored = {"external_reference": "REF-1", "operation_status": "ERP_PENDING", "principal_portion": 383400}
    view = transaction_view(operation(OperationType.REPAY_LOAN, "COMPLETED", stored), {"loan_reference": "LN-2026-000001"})
    assert view.result["operation_status"] == "COMPLETED"
    assert view.result["principal_portion"] == 383400


def test_received_transaction_without_a_stored_body_still_names_its_loan() -> None:
    view = transaction_view(operation(OperationType.DISBURSE_LOAN, "RECEIVED"), {"loan_reference": "LN-2026-000001"})
    assert view.result == {"external_reference": "REF-1", "loan_reference": "LN-2026-000001", "operation_status": "RECEIVED"}


def test_get_transaction(client: TestClient) -> None:
    app.state.store = FakeStore([operation(OperationType.REPAY_LOAN, "FINERACT_DONE")])
    response = client.get("/api/transactions/REF-1", headers=PARTNER)
    assert response.status_code == 200
    body = response.json()
    assert (body["operation_type"], body["operation_status"]) == ("REPAY_LOAN", "FINERACT_DONE")
    assert body["result"]["loan_reference"] == "LN-2026-000001"
    assert set(body) == {"external_reference", "operation_type", "operation_status", "attempts", "created_at", "updated_at", "result"}


def test_unknown_reference_is_404(client: TestClient) -> None:
    app.state.store = FakeStore([])
    response = client.get("/api/transactions/NOPE", headers=PARTNER)
    assert response.status_code == 404
    assert response.json() == {"external_reference": "NOPE", "error": "no transaction NOPE", "code": "not_found"}


def test_reference_shared_by_two_operation_types_needs_operation_type(client: TestClient) -> None:
    app.state.store = FakeStore([operation(OperationType.CREATE_CLIENT, "COMPLETED"), operation(OperationType.REPAY_LOAN, "COMPLETED")])
    response = client.get("/api/transactions/REF-1", headers=PARTNER)
    assert response.status_code == 409
    assert response.json()["code"] == "ambiguous_reference"
    assert response.json()["operation_types"] == ["CREATE_CLIENT", "REPAY_LOAN"]
    chosen = client.get("/api/transactions/REF-1?operation_type=REPAY_LOAN", headers=PARTNER)
    assert (chosen.status_code, chosen.json()["operation_type"]) == (200, "REPAY_LOAN")


def test_unknown_operation_type_is_a_validation_error(client: TestClient) -> None:
    response = client.get("/api/transactions/REF-1?operation_type=NOPE", headers=PARTNER)
    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")


def test_transactions_need_the_partner_key(client: TestClient) -> None:
    assert client.get("/api/transactions/REF-1").status_code == 401
