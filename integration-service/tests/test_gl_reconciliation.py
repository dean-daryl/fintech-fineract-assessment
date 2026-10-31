from typing import Any

from app.domain.gl_mapping import ErpAccount
from app.domain.gl_reconciliation import (
    FineractGLAccount,
    GLReconciliationItem,
    GLReconciliationStatus,
    parse_fineract_gl_account,
    reconcile_gl,
)

CURRENCY = "RWF"


def erp(
    code: str = "1100", name: str = "Cash at Bank", type: str = "ASSET", currency: str = CURRENCY, ref: str | None = None
) -> ErpAccount:
    return ErpAccount(code=code, name=name, type=type, currency=currency, external_reference=ref or f"ERP-ACC-{code}")


def gl(
    code: str = "1100", name: str = "Cash at Bank", type: int = 1, usage: int = 1, ref: str | None = "ERP-ACC-1100"
) -> FineractGLAccount:
    return FineractGLAccount(gl_code=code, name=name, type=type, usage=usage, external_reference=ref)


def run(erp_accounts: list[ErpAccount], fineract_accounts: list[FineractGLAccount]) -> list[GLReconciliationItem]:
    return reconcile_gl(erp_accounts, fineract_accounts, CURRENCY)


def only(items: list[GLReconciliationItem]) -> GLReconciliationItem:
    assert len(items) == 1
    return items[0]


def test_parse_reads_the_fields_the_patch_returns() -> None:
    body: dict[str, Any] = {
        "id": 7,
        "name": "Cash at Bank",
        "glCode": "1100",
        "type": {"id": 1, "code": "accountType.asset", "value": "ASSET"},
        "usage": {"id": 1, "code": "accountUsage.detail", "value": "DETAIL"},
        "externalReference": "ERP-ACC-1100",
    }
    assert parse_fineract_gl_account(body) == gl()


def test_parse_treats_a_missing_reference_as_unlinked() -> None:
    body: dict[str, Any] = {"name": "Petty Cash", "glCode": "1150", "type": {"id": 1}, "usage": {"id": 1}}
    assert parse_fineract_gl_account(body).external_reference is None


def test_identical_accounts_match() -> None:
    item = only(run([erp()], [gl()]))
    assert item.status is GLReconciliationStatus.MATCHED
    assert item.differences == ()
    assert item.erp == erp() and item.fineract == gl()


def test_erp_account_without_fineract_gl_is_missing_in_fineract() -> None:
    item = only(run([erp()], []))
    assert item.status is GLReconciliationStatus.MISSING_IN_FINERACT
    assert item.fineract is None
    assert item.note is None


def test_missing_in_fineract_explains_why_sync_refuses_it() -> None:
    item = only(run([erp(currency="USD")], []))
    assert item.status is GLReconciliationStatus.MISSING_IN_FINERACT
    assert item.note is not None and "USD" in item.note


def test_fineract_gl_without_reference_is_missing_in_erp() -> None:
    item = only(run([], [gl(code="1150", name="Petty Cash", ref=None)]))
    assert item.status is GLReconciliationStatus.MISSING_IN_ERP
    assert item.external_reference is None
    assert item.gl_code == "1150"
    assert item.note is not None and "outside the GL sync" in item.note


def test_fineract_gl_linked_to_an_unknown_reference_is_missing_in_erp() -> None:
    item = only(run([], [gl(ref="ERP-ACC-GONE")]))
    assert item.status is GLReconciliationStatus.MISSING_IN_ERP
    assert item.external_reference == "ERP-ACC-GONE"
    assert item.note is not None and "ERP-ACC-GONE" in item.note


def test_unlinked_fineract_gl_with_the_erp_code_is_one_code_conflict_row() -> None:
    item = only(run([erp()], [gl(name="Cash", ref=None)]))
    assert item.status is GLReconciliationStatus.CODE_CONFLICT
    assert item.external_reference == "ERP-ACC-1100"
    assert item.fineract is not None and item.fineract.external_reference is None
    assert item.note is not None and "no external reference" in item.note


def test_code_held_by_another_linked_account_is_a_conflict_and_the_holder_keeps_its_row() -> None:
    items = run([erp(ref="ERP-ACC-NEW")], [gl(ref="ERP-ACC-OLD")])
    statuses = {(i.external_reference, i.status) for i in items}
    assert statuses == {
        ("ERP-ACC-NEW", GLReconciliationStatus.CODE_CONFLICT),
        ("ERP-ACC-OLD", GLReconciliationStatus.MISSING_IN_ERP),
    }


def test_renamed_account_is_a_details_mismatch() -> None:
    item = only(run([erp()], [gl(name="Cash")]))
    assert item.status is GLReconciliationStatus.DETAILS_MISMATCH
    assert item.differences == ("name: ERP 'Cash at Bank', Fineract 'Cash'",)


def test_every_differing_field_is_listed() -> None:
    item = only(run([erp()], [gl(code="1101", name="Cash", type=5)]))
    assert item.status is GLReconciliationStatus.DETAILS_MISMATCH
    assert item.differences == (
        "code: ERP '1100', Fineract '1101'",
        "name: ERP 'Cash at Bank', Fineract 'Cash'",
        "type: ERP ASSET, Fineract EXPENSE",
    )


def test_linked_account_the_mapping_would_refuse_carries_the_mapping_error() -> None:
    item = only(run([erp(type="CONTRA")], [gl()]))
    assert item.status is GLReconciliationStatus.DETAILS_MISMATCH
    assert item.note is not None and "CONTRA" in item.note


def test_header_accounts_are_ignored() -> None:
    assert run([], [gl(code="1000", name="Assets", usage=2, ref=None)]) == []


def test_rows_are_sorted_by_code() -> None:
    items = run([erp(code="4100", name="Interest Income", type="INCOME"), erp()], [gl()])
    assert [i.gl_code for i in items] == ["1100", "4100"]
