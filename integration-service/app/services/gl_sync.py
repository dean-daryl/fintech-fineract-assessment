import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.erp import ErpClient
from app.clients.fineract import (
    FineractClient,
    FineractError,
    FineractRejectedError,
    FineractTimeoutError,
    FineractUnavailableError,
)
from app.config import Settings
from app.domain.gl_mapping import (
    ErpAccount,
    FineractGLAccountPayload,
    GLMappingError,
    payload_hash,
    to_fineract_gl_account,
)
from app.domain.retry import retry_delay
from app.models import GLAccountSync, GLSyncStatus

log = logging.getLogger(__name__)

RECOVERY_BATCH_SIZE = 100


def _now() -> datetime:
    return datetime.now(UTC)


class GLSyncService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        erp: ErpClient,
        fineract: FineractClient,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._erp = erp
        self._fineract = fineract
        self._settings = settings

    def sync_all(self) -> list[GLAccountSync]:
        accounts = self._erp.list_accounts()
        for account in accounts:
            self._register(account)
        for account in accounts:
            self._process(account.external_reference, due_only=False)
        return self.list_statuses()

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
        with self._sessions() as session, session.begin():
            session.execute(
                insert(GLAccountSync)
                .values(
                    external_reference=account.external_reference,
                    erp_code=account.code,
                    erp_account=asdict(account),
                    payload_hash=payload_hash(account),
                    status=GLSyncStatus.SYNC_PENDING,
                    attempts=0,
                )
                .on_conflict_do_nothing(index_elements=[GLAccountSync.external_reference])
            )

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
            row.status = status
            row.attempts += 1
            row.fineract_gl_account_id = gl_account_id
            row.last_error = error
            row.locked_until = None
        log.info("GL account sync finished", extra={"external_reference": external_reference, "status": status})

    def _retry_later(self, external_reference: str, error: str) -> None:
        with self._sessions() as session, session.begin():
            row = session.get_one(GLAccountSync, external_reference)
            row.attempts += 1
            row.last_error = error
            row.locked_until = None
            if row.attempts >= self._settings.gl_sync_max_attempts:
                row.status = GLSyncStatus.FAILED
            else:
                row.next_attempt_at = _now() + retry_delay(row.attempts)
            status = row.status
        log.warning("GL account sync deferred", extra={"external_reference": external_reference, "status": status})
