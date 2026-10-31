"""Fineract GL accounts against the ERP chart of accounts, keyed by external reference. Pure functions, no I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.gl_mapping import ERP_TO_FINERACT_GL_TYPE, ErpAccount, GLMappingError, to_fineract_gl_account

FINERACT_GL_USAGE_HEADER = 2
FINERACT_TO_ERP_GL_TYPE: dict[int, str] = {value: key for key, value in ERP_TO_FINERACT_GL_TYPE.items()}


class GLReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    CODE_CONFLICT = "CODE_CONFLICT"
    DETAILS_MISMATCH = "DETAILS_MISMATCH"


@dataclass(frozen=True)
class FineractGLAccount:
    gl_code: str
    name: str
    type: int
    usage: int
    external_reference: str | None

    @property
    def type_name(self) -> str:
        return FINERACT_TO_ERP_GL_TYPE.get(self.type, str(self.type))


@dataclass(frozen=True)
class GLReconciliationItem:
    status: GLReconciliationStatus
    external_reference: str | None
    gl_code: str
    erp: ErpAccount | None
    fineract: FineractGLAccount | None
    differences: tuple[str, ...] = ()
    note: str | None = None


def parse_fineract_gl_account(body: dict[str, Any]) -> FineractGLAccount:
    return FineractGLAccount(
        gl_code=str(body["glCode"]),
        name=str(body["name"]),
        type=int(body["type"]["id"]),
        usage=int(body["usage"]["id"]),
        external_reference=body.get("externalReference") or None,
    )


def mapping_error(erp: ErpAccount, tenant_currency: str) -> str | None:
    try:
        to_fineract_gl_account(erp, tenant_currency)
    except GLMappingError as exc:
        return str(exc)
    return None


def differences(erp: ErpAccount, fineract: FineractGLAccount) -> tuple[str, ...]:
    found: list[str] = []
    if erp.code != fineract.gl_code:
        found.append(f"code: ERP {erp.code!r}, Fineract {fineract.gl_code!r}")
    if erp.name != fineract.name:
        found.append(f"name: ERP {erp.name!r}, Fineract {fineract.name!r}")
    if ERP_TO_FINERACT_GL_TYPE.get(erp.type) != fineract.type:
        found.append(f"type: ERP {erp.type}, Fineract {fineract.type_name}")
    return tuple(found)


def _erp_side(
    account: ErpAccount, linked: FineractGLAccount | None, holder: FineractGLAccount | None, tenant_currency: str
) -> GLReconciliationItem:
    reference, code = account.external_reference, account.code
    error = mapping_error(account, tenant_currency)
    if linked is not None:
        diffs = differences(account, linked)
        status = GLReconciliationStatus.DETAILS_MISMATCH if diffs else GLReconciliationStatus.MATCHED
        return GLReconciliationItem(status, reference, code, account, linked, diffs, error)
    if holder is not None:
        owner = f"linked to {holder.external_reference}" if holder.external_reference else "with no external reference"
        note = f"Fineract GL account {code} already exists {owner}; sync never adopts an existing account"
        return GLReconciliationItem(GLReconciliationStatus.CODE_CONFLICT, reference, code, account, holder, (), note)
    return GLReconciliationItem(GLReconciliationStatus.MISSING_IN_FINERACT, reference, code, account, None, (), error)


def reconcile_gl(
    erp_accounts: Iterable[ErpAccount], fineract_accounts: Iterable[FineractGLAccount], tenant_currency: str
) -> list[GLReconciliationItem]:
    erp = list(erp_accounts)
    detail = [f for f in fineract_accounts if f.usage != FINERACT_GL_USAGE_HEADER]
    by_reference = {f.external_reference: f for f in detail if f.external_reference}
    by_code = {f.gl_code: f for f in detail}
    erp_references = {a.external_reference for a in erp}
    items = [
        _erp_side(account, by_reference.get(account.external_reference), by_code.get(account.code), tenant_currency) for account in erp
    ]
    conflicted = {i.gl_code for i in items if i.status is GLReconciliationStatus.CODE_CONFLICT}

    for gl in detail:
        if gl.external_reference is None:
            if gl.gl_code in conflicted:
                continue
            note = "no external reference: created in Fineract outside the GL sync"
        elif gl.external_reference not in erp_references:
            note = f"linked to {gl.external_reference}, which the ERP does not hold"
        else:
            continue
        items.append(GLReconciliationItem(GLReconciliationStatus.MISSING_IN_ERP, gl.external_reference, gl.gl_code, None, gl, (), note))

    return sorted(items, key=lambda item: (item.gl_code, item.external_reference or ""))
