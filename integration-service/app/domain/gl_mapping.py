"""ERP account to Fineract GL account mapping. Pure functions, no I/O."""

import hashlib
import json
from dataclasses import asdict, dataclass

ERP_TO_FINERACT_GL_TYPE: dict[str, int] = {
    "ASSET": 1,
    "LIABILITY": 2,
    "EQUITY": 3,
    "INCOME": 4,
    "EXPENSE": 5,
}

FINERACT_GL_USAGE_DETAIL = 1
FINERACT_GL_NAME_MAX_LENGTH = 45


class GLMappingError(ValueError):
    pass


@dataclass(frozen=True)
class ErpAccount:
    code: str
    name: str
    type: str
    currency: str
    external_reference: str


@dataclass(frozen=True)
class FineractGLAccountPayload:
    externalReference: str
    glCode: str
    name: str
    type: int
    usage: int
    manualEntriesAllowed: bool


def to_fineract_gl_type(erp_type: str) -> int:
    try:
        return ERP_TO_FINERACT_GL_TYPE[erp_type]
    except KeyError:
        raise GLMappingError(f"ERP account type {erp_type!r} has no Fineract GL type") from None


def to_fineract_gl_account(account: ErpAccount, tenant_currency: str) -> FineractGLAccountPayload:
    if account.currency != tenant_currency:
        raise GLMappingError(f"account currency {account.currency} differs from the Fineract tenant currency {tenant_currency}")
    if len(account.name) > FINERACT_GL_NAME_MAX_LENGTH:
        raise GLMappingError(f"account name is longer than Fineract's {FINERACT_GL_NAME_MAX_LENGTH} characters")
    return FineractGLAccountPayload(
        externalReference=account.external_reference,
        glCode=account.code,
        name=account.name,
        type=to_fineract_gl_type(account.type),
        usage=FINERACT_GL_USAGE_DETAIL,
        manualEntriesAllowed=True,
    )


def payload_hash(account: ErpAccount) -> str:
    return hashlib.sha256(json.dumps(asdict(account), sort_keys=True).encode()).hexdigest()
