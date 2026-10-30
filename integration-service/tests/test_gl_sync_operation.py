from datetime import UTC, datetime
from typing import Any

from app.clients.erp import ErpUnavailableError
from app.config import Settings
from app.models import GLAccountSync, GLSyncStatus
from app.services.gl_sync import GLAccountsSync
from app.services.pipeline import OperationPipeline
from tests.test_pipeline import FakeStore


class FakeGLSyncService:
    def __init__(self, erp_down: bool = False) -> None:
        self.erp_down = erp_down
        self.registered = 0
        self.processed = 0

    def register_all(self) -> None:
        if self.erp_down:
            raise ErpUnavailableError("ERP account list failed: connection refused")
        self.registered += 1

    def process_pending(self) -> None:
        self.processed += 1

    def list_statuses(self) -> list[GLAccountSync]:
        return [
            GLAccountSync(
                external_reference="GL-1200",
                erp_code="1200",
                status=GLSyncStatus.SYNCED.value,
                attempts=1,
                last_error=None,
                updated_at=datetime(2026, 10, 30, tzinfo=UTC),
            )
        ]


def _submit(service: FakeGLSyncService, store: FakeStore, reference: str) -> tuple[int, dict[str, Any]]:
    settings = Settings(database_url="x", erp_base_url="x", fineract_base_url="x", fineract_username="x", fineract_password="x")
    pipeline = OperationPipeline(store, settings)  # type: ignore[arg-type]
    result = pipeline.submit(GLAccountsSync(service), reference, {})  # type: ignore[arg-type]
    return result.status_code, result.body


def test_sync_runs_once_per_reference_and_replays_the_same_body() -> None:
    service, store = FakeGLSyncService(), FakeStore()
    first = _submit(service, store, "SYNC-1")
    again = _submit(service, store, "SYNC-1")
    assert first[0] == 201 and first[1]["operation_status"] == "COMPLETED"
    assert first[1]["accounts"][0] == {
        "external_reference": "GL-1200",
        "erp_code": "1200",
        "status": "SYNCED",
        "attempts": 1,
        "last_error": None,
        "updated_at": "2026-10-30T00:00:00+00:00",
    }
    assert again == (200, first[1])
    assert (service.registered, service.processed) == (1, 1)


def test_new_reference_runs_a_new_sync() -> None:
    service, store = FakeGLSyncService(), FakeStore()
    _submit(service, store, "SYNC-1")
    code, _ = _submit(service, store, "SYNC-2")
    assert code == 201 and service.processed == 2


def test_erp_down_fails_with_503_and_replays_the_failure() -> None:
    service, store = FakeGLSyncService(erp_down=True), FakeStore()
    code, body = _submit(service, store, "SYNC-1")
    assert code == 503 and body["operation_status"] == "FAILED"
    service.erp_down = False
    assert _submit(service, store, "SYNC-1") == (503, body)
    assert service.processed == 0
