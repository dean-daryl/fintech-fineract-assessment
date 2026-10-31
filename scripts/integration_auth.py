"""X-API-Key headers for calls to the integration service: the admin key for /admin routes, the partner key otherwise."""

import os
import urllib.parse

INTEGRATION_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8000")


def integration_headers(url: str) -> dict[str, str]:
    if not url.startswith(INTEGRATION_URL):
        return {}
    path = urllib.parse.urlsplit(url).path
    if path == "/health":
        return {}
    key = os.environ.get("ADMIN_API_KEY" if path.startswith("/admin") else "PARTNER_API_KEY", "")
    return {"X-API-Key": key}
