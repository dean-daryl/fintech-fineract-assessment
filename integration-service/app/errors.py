"""One error shape for every error response: {"error": message, "code": machine code, ...context}."""

from collections.abc import Sequence
from typing import Any

ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "unprocessable",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
}

INVALID_REQUEST = "invalid_request"
VALIDATION_LOCATIONS = {"body", "query", "path", "header"}


def error_code(status_code: int) -> str:
    return ERROR_CODES.get(status_code, "client_error" if status_code < 500 else "server_error")


def error_body(status_code: int, message: str, code: str | None = None, **context: Any) -> dict[str, Any]:
    return {**context, "error": message, "code": code or error_code(status_code)}


def validation_details(errors: Sequence[Any]) -> list[dict[str, str]]:
    details = []
    for error in errors:
        location = [str(part) for part in error.get("loc", ())]
        source = location[0] if location and location[0] in VALIDATION_LOCATIONS else "body"
        field = ".".join(location[1:] if location and location[0] in VALIDATION_LOCATIONS else location)
        details.append({"in": source, "field": field, "message": str(error.get("msg", ""))})
    return details
