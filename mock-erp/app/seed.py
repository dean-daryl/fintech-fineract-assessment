import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account

log = logging.getLogger(__name__)

CURRENCY = "RWF"

CHART_OF_ACCOUNTS: list[dict[str, str]] = [
    {"code": "1100", "name": "Cash at Bank", "type": "ASSET"},
    {"code": "1200", "name": "Loan Receivable", "type": "ASSET"},
    {"code": "1300", "name": "Transfers in Suspense", "type": "ASSET"},
    {"code": "2100", "name": "Loan Overpayments", "type": "LIABILITY"},
    {"code": "4100", "name": "Interest Income on Loans", "type": "INCOME"},
    {"code": "4200", "name": "Fee Income", "type": "INCOME"},
    {"code": "4300", "name": "Penalty Income", "type": "INCOME"},
    {"code": "4400", "name": "Recoveries Income", "type": "INCOME"},
    {"code": "5100", "name": "Loan Losses Written Off", "type": "EXPENSE"},
]


def seed_accounts() -> list[dict[str, str]]:
    return [{**account, "currency": CURRENCY, "external_reference": f"GL-{account['code']}"} for account in CHART_OF_ACCOUNTS]


def seed_chart_of_accounts(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, session.begin():
        for account in seed_accounts():
            inserted = session.scalar(insert(Account).values(**account).on_conflict_do_nothing().returning(Account.id))
            if inserted is not None:
                log.info("Seeded account %s", account["external_reference"])
                continue
            existing = session.scalar(select(Account).where(Account.external_reference == account["external_reference"]))
            if existing is None or {key: getattr(existing, key) for key in account} != account:
                log.warning("Seed account %s conflicts with an existing account; left unchanged", account["external_reference"])
