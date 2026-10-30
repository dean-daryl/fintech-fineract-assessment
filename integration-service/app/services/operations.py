import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.domain.idempotency import payload_hash
from app.domain.retry import retry_delay
from app.domain.states import FINISHED_STATUSES, OperationStatus, OperationType, check_transition, is_finished
from app.models import Operation

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Claim:
    operation: Operation
    created: bool


class OperationStore:
    def __init__(self, session_factory: sessionmaker[Session], lease_seconds: int) -> None:
        self._sessions = session_factory
        self._lease = timedelta(seconds=lease_seconds)

    def claim(self, operation_type: OperationType, external_reference: str, payload: dict[str, Any]) -> Claim:
        with self._sessions() as session, session.begin():
            created = session.scalars(
                insert(Operation)
                .values(
                    operation_type=operation_type,
                    external_reference=external_reference,
                    payload=payload,
                    payload_hash=payload_hash(payload),
                    status=OperationStatus.RECEIVED,
                    attempts=0,
                    locked_until=_now() + self._lease,
                )
                .on_conflict_do_nothing(constraint="uq_operations_type_reference")
                .returning(Operation)
            ).first()
            if created is not None:
                log.info("Operation received", extra=self._log_fields(created))
                return Claim(created, created=True)
            existing = session.scalars(
                select(Operation).where(
                    Operation.operation_type == operation_type,
                    Operation.external_reference == external_reference,
                )
            ).one()
            return Claim(existing, created=False)

    def transition(
        self,
        operation_id: int,
        target: OperationStatus,
        *,
        fineract_resource_id: int | None = None,
        erp_transaction_id: str | None = None,
        response_code: int | None = None,
        response: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> Operation:
        with self._sessions() as session, session.begin():
            row = session.get_one(Operation, operation_id, with_for_update=True)
            check_transition(OperationType(row.operation_type), OperationStatus(row.status), target)
            row.status = target
            if fineract_resource_id is not None:
                row.fineract_resource_id = fineract_resource_id
            if erp_transaction_id is not None:
                row.erp_transaction_id = erp_transaction_id
            if last_error is not None:
                row.last_error = last_error
            if is_finished(target):
                row.response_code = response_code
                row.response = response
                row.locked_until = None
        log.info("Operation status changed", extra=self._log_fields(row))
        return row

    def defer(self, operation_id: int, error: str) -> Operation:
        with self._sessions() as session, session.begin():
            row = session.get_one(Operation, operation_id, with_for_update=True)
            row.attempts += 1
            row.last_error = error
            row.next_attempt_at = _now() + retry_delay(row.attempts)
            row.locked_until = None
        log.warning("Operation deferred", extra=self._log_fields(row))
        return row

    def claim_due(self, operation_types: Collection[OperationType], limit: int) -> list[Operation]:
        now = _now()
        with self._sessions() as session, session.begin():
            rows = list(
                session.scalars(
                    select(Operation)
                    .where(
                        Operation.operation_type.in_(operation_types),
                        Operation.status.not_in(FINISHED_STATUSES),
                        Operation.next_attempt_at <= now,
                        or_(Operation.locked_until.is_(None), Operation.locked_until < now),
                    )
                    .order_by(Operation.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.locked_until = now + self._lease
        return rows

    def get(self, operation_type: OperationType, external_reference: str) -> Operation | None:
        with self._sessions() as session:
            return session.scalars(
                select(Operation).where(
                    Operation.operation_type == operation_type,
                    Operation.external_reference == external_reference,
                )
            ).first()

    @staticmethod
    def _log_fields(row: Operation) -> dict[str, Any]:
        return {"external_reference": row.external_reference, "status": row.status}
