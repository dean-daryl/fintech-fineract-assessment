import httpx

from app.domain.gl_mapping import ErpAccount


class ErpUnavailableError(Exception):
    pass


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
