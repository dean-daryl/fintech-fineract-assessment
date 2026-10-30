from dataclasses import asdict
from typing import Any

import httpx

from app.domain.clients import FineractClientPayload, FineractClientRecord, parse_fineract_client
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
        self.code: str | None = None
        self.message = str(body)
        if isinstance(body, dict):
            errors = body.get("errors") or [{}]
            self.code = errors[0].get("userMessageGlobalisationCode") or body.get("userMessageGlobalisationCode")
            self.message = str(errors[0].get("defaultUserMessage") or body.get("defaultUserMessage") or body)
        top_level_code = body.get("userMessageGlobalisationCode") if isinstance(body, dict) else None
        super().__init__(f"Fineract returned {status_code}: {top_level_code or body}")


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

    def find_office_id(self, name: str) -> int | None:
        offices = self._request_list("GET", "/offices")
        return next((int(office["id"]) for office in offices if office.get("name") == name), None)

    def create_client(self, payload: FineractClientPayload) -> int:
        body = self._request("POST", "/clients", json=asdict(payload))
        return int(body["resourceId"])

    def find_client_by_external_id(self, external_id: str) -> FineractClientRecord | None:
        try:
            body = self._request("GET", f"/clients/external-id/{external_id}")
        except FineractRejectedError as exc:
            if exc.status_code == 404:
                return None
            raise
        return parse_fineract_client(body)

    def _request_list(self, method: str, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._send(method, path, None).json()
        return result

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self._send(method, path, json).json()
        return result

    def _send(self, method: str, path: str, json: dict[str, Any] | None) -> httpx.Response:
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
        return response
