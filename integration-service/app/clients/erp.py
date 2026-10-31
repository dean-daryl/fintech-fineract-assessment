from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.domain.gl_mapping import ErpAccount


class ErpError(Exception):
    pass


class ErpUnavailableError(ErpError):
    """The request did not reach the ERP or the ERP could not serve it; safe to resend."""


class ErpTimeoutError(ErpError):
    """The outcome is unknown; look the journal entry up before resending."""


class ErpRejectedError(ErpError):
    """The ERP refused the request (4xx); resending will not help."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"ERP returned {status_code}: {detail}")


@dataclass(frozen=True)
class ErpJournalEntry:
    erp_transaction_id: str
    request: dict[str, Any]


class ErpClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout_seconds)

    def list_accounts(self) -> list[ErpAccount]:
        try:
            response = self._http.get("/api/accounts")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ErpUnavailableError(f"ERP account list failed: {exc}") from exc
        return [
            ErpAccount(
                code=item["code"],
                name=item["name"],
                type=item["type"],
                currency=item["currency"],
                external_reference=item["external_reference"],
            )
            for item in response.json()
        ]

    def post_journal_entry(self, request: dict[str, Any]) -> ErpJournalEntry:
        return _journal_entry(self._send("POST", "/api/journal-entries", request).json())

    def find_journal_entry(self, external_reference: str) -> ErpJournalEntry | None:
        try:
            body = self._send("GET", f"/api/journal-entries/{external_reference}", None).json()
        except ErpRejectedError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _journal_entry(body)

    def list_journal_entries(self, from_date: date, to_date: date) -> list[ErpJournalEntry]:
        params = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
        return [_journal_entry(body) for body in self._send("GET", "/api/journal-entries", None, params).json()]

    def _send(self, method: str, path: str, json: dict[str, Any] | None, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self._http.request(method, path, json=json, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ErpUnavailableError(f"{method} {path}: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise ErpTimeoutError(f"{method} {path}: {exc!r}") from exc

        if response.status_code in (502, 503):
            raise ErpUnavailableError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 500:
            raise ErpTimeoutError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail"))
            except ValueError:
                detail = response.text
            raise ErpRejectedError(response.status_code, detail)
        return response


def _journal_entry(body: dict[str, Any]) -> ErpJournalEntry:
    return ErpJournalEntry(
        erp_transaction_id=str(body["id"]),
        request={
            "external_reference": body["external_reference"],
            "entry_date": body["entry_date"],
            "description": body["description"],
            "currency": body["currency"],
            "lines": [{"account_code": line["account_code"], "debit": line["debit"], "credit": line["credit"]} for line in body["lines"]],
        },
    )
