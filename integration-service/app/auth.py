import hmac

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import get_settings

API_KEY_HEADER = "X-API-Key"

_partner_key = APIKeyHeader(name=API_KEY_HEADER, scheme_name="PartnerApiKey", auto_error=False)
_admin_key = APIKeyHeader(name=API_KEY_HEADER, scheme_name="AdminApiKey", auto_error=False)


def key_matches(presented: str | None, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        f"missing or invalid {API_KEY_HEADER}",
        headers={"WWW-Authenticate": API_KEY_HEADER},
    )


def require_partner_key(api_key: str | None = Security(_partner_key)) -> None:
    if not key_matches(api_key, get_settings().partner_api_key.get_secret_value()):
        raise _unauthorized()


def require_admin_key(api_key: str | None = Security(_admin_key)) -> None:
    if not key_matches(api_key, get_settings().admin_api_key.get_secret_value()):
        raise _unauthorized()
