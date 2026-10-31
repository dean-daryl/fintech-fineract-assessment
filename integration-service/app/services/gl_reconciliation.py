import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.erp import ErpClient
from app.clients.fineract import FineractClient
from app.domain.gl_reconciliation import GLReconciliationItem, reconcile_gl
from app.models import GLAccountSync

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GLReconciliationReport:
    items: list[GLReconciliationItem]
    sync_statuses: dict[str, str]


class GLReconciliationService:
    def __init__(self, session_factory: sessionmaker[Session], fineract: FineractClient, erp: ErpClient, tenant_currency: str) -> None:
        self._sessions = session_factory
        self._fineract = fineract
        self._erp = erp
        self._tenant_currency = tenant_currency

    def report(self) -> GLReconciliationReport:
        erp_accounts = self._erp.list_accounts()
        fineract_accounts = self._fineract.list_gl_accounts()
        items = reconcile_gl(erp_accounts, fineract_accounts, self._tenant_currency)
        with self._sessions() as session:
            sync_statuses = {row.external_reference: row.status for row in session.scalars(select(GLAccountSync))}
        counts: dict[str, int] = {}
        for item in items:
            counts[item.status] = counts.get(item.status, 0) + 1
        log.info("GL reconciliation: %s", ", ".join(f"{status} {count}" for status, count in sorted(counts.items())))
        return GLReconciliationReport(items, sync_statuses)
