import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.erp import ErpClient, ErpUnavailableError
from app.clients.fineract import (
    FineractClient,
    FineractError,
    FineractRejectedError,
    FineractTimeoutError,
    FineractUnavailableError,
)
from app.config import Settings
from app.domain.accounting import JournalEntry
from app.domain.gl_mapping import (
    ErpAccount,
    FineractGLAccountPayload,
    GLMappingError,
    payload_hash,
    to_fineract_gl_account,
)
from app.domain.retry import retry_delay
from app.domain.states import OperationType
from app.models import GLAccountSync, GLSyncStatus, Operation
from app.services.pipeline import FineractOutcome, OperationHandler, OperationPipeline, OperationRejected, OperationResult

log = logging.getLogger(__name__)

RECOVERY_BATCH_SIZE = 100
ERP_DETAILS_CHANGED = "ERP account details changed after the first sync; sync is create-only"


def _now() -> datetime:
    return datetime.now(UTC)


def account_view(row: GLAccountSync) -> dict[str, Any]:
    return {
        "external_reference": row.external_reference,
        "erp_code": row.erp_code,
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "updated_at": row.updated_at.isoformat(),
    }


class GLAccountsSync:
    operation_type = OperationType.SYNC_GL_ACCOUNTS

    def __init__(self, service: "GLSyncService") -> None:
        self._service = service

    def prepare(self, operation: Operation) -> None:
        try:
            self._service.register_all()
        except ErpUnavailableError as exc:
            raise OperationRejected(503, str(exc)) from exc

    def look_up(self, operation: Operation) -> FineractOutcome | None:
        return None

    def execute(self, operation: Operation) -> FineractOutcome:
        self._service.process_pending()
        return FineractOutcome(None, {"accounts": [account_view(row) for row in self._service.list_statuses()]})

    def record(self, operation: Operation, outcome: FineractOutcome) -> None:
        return None

    def journal_entry(self, operation: Operation, outcome: FineractOutcome) -> JournalEntry | None:
        return None

    def describe(self, operation: Operation) -> dict[str, Any]:
        return {}


class GLSyncService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        erp: ErpClient,
        fineract: FineractClient,
        pipeline: OperationPipeline,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._erp = erp
        self._fineract = fineract
        self._pipeline = pipeline
        self._settings = settings
        self._handler = GLAccountsSync(self)

    @property
    def handlers(self) -> list[OperationHandler]:
        return [self._handler]

    def sync(self, external_reference: str) -> OperationResult:
        return self._pipeline.submit(self._handler, external_reference, {})

    def register_all(self) -> None:
        for account in self._erp.list_accounts():
            self._register(account)

    def process_pending(self) -> None:
        with self._sessions() as session:
            references = session.scalars(
                select(GLAccountSync.external_reference)
                .where(GLAccountSync.status == GLSyncStatus.SYNC_PENDING)
                .order_by(GLAccountSync.created_at)
            ).all()
        for reference in references:
            self._process(reference, due_only=False)

    def recover(self) -> int:
        with self._sessions() as session:
            references = session.scalars(
                select(GLAccountSync.external_reference)
                .where(
                    GLAccountSync.status == GLSyncStatus.SYNC_PENDING,
                    GLAccountSync.next_attempt_at <= _now(),
                    or_(GLAccountSync.locked_until.is_(None), GLAccountSync.locked_until < _now()),
                )
                .order_by(GLAccountSync.next_attempt_at)
                .limit(RECOVERY_BATCH_SIZE)
            ).all()
        return sum(self._process(reference, due_only=True) for reference in references)

    def list_statuses(self) -> list[GLAccountSync]:
        with self._sessions() as session:
            return list(session.scalars(select(GLAccountSync).order_by(GLAccountSync.created_at)))

    def _register(self, account: ErpAccount) -> None:
        account_hash = payload_hash(account)
        with self._sessions() as session, session.begin():
            inserted = session.scalar(
                insert(GLAccountSync)
                .values(
                    external_reference=account.external_reference,
                    erp_code=account.code,
                    erp_account=asdict(account),
                    payload_hash=account_hash,
                    status=GLSyncStatus.SYNC_PENDING,
                    attempts=0,
                )
                .on_conflict_do_nothing()
                .returning(GLAccountSync.external_reference)
            )
            if inserted is not None:
                return
            row = session.get(GLAccountSync, account.external_reference, with_for_update=True)
            if row is None:
                log.error(
                    "ERP account code is already used by another synced account",
                    extra={"external_reference": account.external_reference, "status": GLSyncStatus.CONFLICT},
                )
                return
            if row.payload_hash == account_hash or row.status == GLSyncStatus.CONFLICT:
                return
            if row.status == GLSyncStatus.FAILED:
                row.erp_code = account.code
                row.erp_account = asdict(account)
                row.payload_hash = account_hash
                row.status = GLSyncStatus.SYNC_PENDING
                row.attempts = 0
                row.next_attempt_at = _now()
                row.last_error = None
                return
            row.status = GLSyncStatus.CONFLICT
            row.last_error = ERP_DETAILS_CHANGED
        log.warning(ERP_DETAILS_CHANGED, extra={"external_reference": account.external_reference, "status": GLSyncStatus.CONFLICT})

    def _claim(self, external_reference: str, due_only: bool) -> ErpAccount | None:
        now = _now()
        with self._sessions() as session, session.begin():
            query = select(GLAccountSync).where(
                GLAccountSync.external_reference == external_reference,
                GLAccountSync.status == GLSyncStatus.SYNC_PENDING,
                or_(GLAccountSync.locked_until.is_(None), GLAccountSync.locked_until < now),
            )
            if due_only:
                query = query.where(GLAccountSync.next_attempt_at <= now)
            row = session.scalar(query.with_for_update(skip_locked=True))
            if row is None:
                return None
            row.locked_until = now + timedelta(seconds=self._settings.gl_sync_lease_seconds)
            return ErpAccount(**row.erp_account)

    def _process(self, external_reference: str, due_only: bool) -> bool:
        account = self._claim(external_reference, due_only)
        if account is None:
            return False

        try:
            payload = to_fineract_gl_account(account, self._settings.tenant_currency)
            gl_account_id = self._push(payload)
        except (GLMappingError, FineractRejectedError) as exc:
            self._finish(external_reference, GLSyncStatus.FAILED, error=str(exc))
        except (FineractUnavailableError, FineractTimeoutError) as exc:
            self._retry_later(external_reference, str(exc))
        else:
            self._finish(external_reference, GLSyncStatus.SYNCED, gl_account_id=gl_account_id)
        return True

    def _push(self, payload: FineractGLAccountPayload) -> int:
        last_error: FineractError | None = None
        for attempt in range(self._settings.gl_sync_inline_attempts):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            try:
                if isinstance(last_error, FineractTimeoutError):
                    existing_id = self._fineract.find_gl_account_id_by_reference(payload.externalReference)
                    if existing_id is not None:
                        return existing_id
                return self._fineract.sync_gl_account(payload)
            except (FineractUnavailableError, FineractTimeoutError) as exc:
                last_error = exc
                log.warning(
                    "Fineract GL sync attempt failed",
                    extra={"external_reference": payload.externalReference, "status": GLSyncStatus.SYNC_PENDING},
                )
        assert last_error is not None
        raise last_error

    def _finish(
        self,
        external_reference: str,
        status: GLSyncStatus,
        gl_account_id: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._sessions() as session, session.begin():
            row = session.get_one(GLAccountSync, external_reference)
            row.attempts += 1
            row.fineract_gl_account_id = gl_account_id
            row.locked_until = None
            if row.status != GLSyncStatus.CONFLICT:
                row.status = status
                row.last_error = error
            status = GLSyncStatus(row.status)
        log.info("GL account sync finished", extra={"external_reference": external_reference, "status": status})

    def _retry_later(self, external_reference: str, error: str) -> None:
        with self._sessions() as session, session.begin():
            row = session.get_one(GLAccountSync, external_reference)
            row.attempts += 1
            row.locked_until = None
            if row.status != GLSyncStatus.CONFLICT:
                row.last_error = error
                if row.attempts >= self._settings.gl_sync_max_attempts:
                    row.status = GLSyncStatus.FAILED
                else:
                    row.next_attempt_at = _now() + retry_delay(row.attempts)
            status = row.status
        log.warning("GL account sync deferred", extra={"external_reference": external_reference, "status": status})
