from dataclasses import asdict
from typing import Any

import httpx

from app.domain.gl_mapping import FineractGLAccountPayload


class FineractError(Exception):
    pass


class FineractUnavailableError(FineractError):
    """The request did not reach Fineract or Fineract could not serve it; safe to resend."""


class FineractTimeoutError(FineractError):
    """The outcome is unknown; look the resource up before resending."""


class FineractRejectedError(FineractError):
    """Fineract refused the request (4xx); resending will not help."""

    def __init__(self, status_code: int, body: dict[str, Any] | str) -> None:
        self.status_code = status_code
        self.body = body
        code = body.get("userMessageGlobalisationCode") if isinstance(body, dict) else None
        super().__init__(f"Fineract returned {status_code}: {code or body}")


class FineractClient:
    def __init__(
        self,
        base_url: str,
        tenant: str,
        username: str,
        password: str,
        verify_tls: bool,
        timeout_seconds: float,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            auth=(username, password),
            headers={"Fineract-Platform-TenantId": tenant},
            verify=verify_tls,
            timeout=timeout_seconds,
        )

    def sync_gl_account(self, payload: FineractGLAccountPayload) -> int:
        body = self._request("POST", "/glaccounts/sync", json=asdict(payload))
        return int(body["resourceId"])

    def find_gl_account_id_by_reference(self, external_reference: str) -> int | None:
        try:
            body = self._request("GET", f"/glaccounts/external-reference/{external_reference}")
        except FineractRejectedError as exc:
            if exc.status_code == 404:
                return None
            raise
        return int(body["id"])

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=json)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise FineractUnavailableError(f"{method} {path}: {exc!r}") from exc
        except httpx.TimeoutException as exc:
            raise FineractTimeoutError(f"{method} {path}: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise FineractTimeoutError(f"{method} {path}: {exc!r}") from exc

        if response.status_code in (502, 503):
            raise FineractUnavailableError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 500:
            raise FineractTimeoutError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            try:
                error_body: dict[str, Any] | str = response.json()
            except ValueError:
                error_body = response.text
            raise FineractRejectedError(response.status_code, error_body)
        result: dict[str, Any] = response.json()
        return result
